#!/usr/bin/python
"""Access Juju CI artifacts and data."""

from __future__ import print_function

from argparse import ArgumentParser
import base64
from collections import namedtuple
import fnmatch
import json
import os
import shutil
import sys
import traceback
import urllib
import urllib2

from deploy_stack import destroy_environment
from jujuconfig import NoSuchEnvironment
from jujupy import (
    SimpleEnvironment,
    EnvJujuClient,
)
from lsb_release import get_distro_information
from utility import get_deb_arch


__metaclass__ = type


JENKINS_URL = 'http://juju-ci.vapour.ws:8080'
BUILD_REVISION = 'build-revision'
PUBLISH_REVISION = 'publish-revision'

Artifact = namedtuple('Artifact', ['file_name', 'location'])


def print_now(string):
    print(string)


Credentials = namedtuple('Credentials', ['user', 'password'])


def get_build_data(jenkins_url, credentials, job_name,
                   build='lastSuccessfulBuild'):
    """Return a dict of the build data for a job build number."""
    req = urllib2.Request(
        '%s/job/%s/%s/api/json' % (jenkins_url, job_name, build))

    encoded = base64.encodestring(
        '{}:{}'.format(*credentials)).replace('\n', '')
    req.add_header('Authorization', 'Basic {}'.format(encoded))
    build_data = urllib2.urlopen(req)
    build_data = json.load(build_data)
    return build_data


def make_artifact(build_data, artifact):
    location = '%sartifact/%s' % (build_data['url'], artifact['relativePath'])
    return Artifact(artifact['fileName'], location)


def find_artifacts(build_data, glob='*'):
    found = []
    for artifact in build_data['artifacts']:
        file_name = artifact['fileName']
        if fnmatch.fnmatch(file_name, glob):
            found.append(make_artifact(build_data, artifact))
    return found


def list_artifacts(credentials, job_name, build, glob, verbose=False):
    build_data = get_build_data(JENKINS_URL, credentials, job_name, build)
    artifacts = find_artifacts(build_data, glob)
    for artifact in artifacts:
        if verbose:
            print_now(artifact.location)
        else:
            print_now(artifact.file_name)


def retrieve_artifact(credentials, url, local_path, opener=None):
    if opener is None:
        opener = urllib.URLopener()
    auth_location = url.replace('http://',
                                'http://{}:{}@'.format(*credentials))
    opener.retrieve(auth_location, local_path)


def get_juju_bin_artifact(package_namer, version, build_data):
    file_name = package_namer.get_release_package(version)
    by_filename = dict((a['fileName'], a) for a in build_data['artifacts'])
    bin_artifact = by_filename[file_name]
    return make_artifact(build_data, bin_artifact)


def get_juju_bin(credentials, workspace, version):
    build_data = get_build_data(JENKINS_URL, credentials, PUBLISH_REVISION,
                                'lastBuild')
    namer = PackageNamer.factory()
    artifact = get_juju_bin_artifact(namer, version, build_data)
    target_path = os.path.join(workspace, artifact.file_name)
    retrieve_artifact(credentials, artifact.location, target_path)


def get_artifacts(credentials, job_name, build, glob, path,
                  archive=False, dry_run=False, verbose=False):
    full_path = os.path.expanduser(path)
    if archive:
        if verbose:
            print_now('Cleaning %s' % full_path)
        if not os.path.isdir(full_path):
            raise ValueError('%s does not exist' % full_path)
        shutil.rmtree(full_path)
        os.makedirs(full_path)
    build_data = get_build_data(JENKINS_URL, credentials, job_name, build)
    artifacts = find_artifacts(build_data, glob)
    opener = urllib.URLopener()
    for artifact in artifacts:
        local_path = os.path.abspath(
            os.path.join(full_path, artifact.file_name))
        if verbose:
            print_now('Retrieving %s => %s' % (artifact.location, local_path))
        else:
            print_now(artifact.file_name)
        if not dry_run:
            retrieve_artifact(credentials, artifact.location, local_path,
                              opener)
    return artifacts


def clean_environment(env_name, verbose=False):
    try:
        env = SimpleEnvironment.from_config(env_name)
    except NoSuchEnvironment as e:
        # Nothing to do.
        if verbose:
            print_now(str(e))
        return False
    client = EnvJujuClient.by_version(env)
    if verbose:
        print_now("Destroying %s" % env_name)
    destroy_environment(client, env_name)
    return True


def setup_workspace(workspace_dir, env=None, dry_run=False, verbose=False):
    """Clean the workspace directory and create an artifacts sub directory."""
    for root, dirs, files in os.walk(workspace_dir):
        for name in files:
            print_now('Removing %s' % name)
            if not dry_run:
                os.remove(os.path.join(root, name))
        for name in dirs:
            print_now('Removing %s' % name)
            if not dry_run:
                shutil.rmtree(os.path.join(root, name))
    artifacts_path = os.path.join(workspace_dir, 'artifacts')
    print_now('Creating artifacts dir.')
    if not dry_run:
        os.mkdir(artifacts_path)
    # "touch empty" to convince jenkins there is an archive.
    empty_path = os.path.join(artifacts_path, 'empty')
    if not dry_run:
        with open(empty_path, 'w'):
            pass
    if env is not None and not dry_run:
        clean_environment(env, verbose=verbose)


def add_artifacts(workspace_dir, globs, dry_run=False, verbose=False):
    """Find files beneath the workspace_dir and move them to the artifacts.

    The list of globs can match the full file name, part of a name, or
    a sub directory: eg: buildvars.json, *.deb, tmp/*.deb.
    """
    workspace_dir = os.path.realpath(workspace_dir)
    artifacts_dir = os.path.join(workspace_dir, 'artifacts')
    for root, dirs, files in os.walk(workspace_dir):
        # create a pseudo-relative path to make glob matches easy.
        relative = os.path.relpath(root, workspace_dir)
        if relative == '.':
            relative = ''
        if 'artifacts' in dirs:
            dirs.remove('artifacts')
        for file_name in files:
            file_path = os.path.join(root, file_name)
            file_relative_path = os.path.join(relative, file_name)
            for glob in globs:
                if fnmatch.fnmatch(file_relative_path, glob):
                    if verbose:
                        print_now("Adding artifact %s" % file_relative_path)
                    if not dry_run:
                        shutil.move(file_path, artifacts_dir)
                    break


def add_build_job_glob(parser):
    """Added the --build, job, and glob arguments to the parser."""
    parser.add_argument(
        '-b', '--build', default='lastSuccessfulBuild',
        help="The specific build to examine (default: lastSuccessfulBuild).")
    parser.add_argument(
        'job', help="The job that collected the artifacts.")
    parser.add_argument(
        'glob', nargs='?', default='*',
        help="The glob pattern to match artifact file names.")


def add_credential_args(parser):
    parser.add_argument(
        '--user', default=os.environ.get('JENKINS_USER'))
    parser.add_argument(
        '--password', default=os.environ.get('JENKINS_PASSWORD'))


def parse_args(args=None):
    """Return the argument parser for this program."""
    parser = ArgumentParser("List and get artifacts from Juju CI.")
    parser.add_argument(
        '-d', '--dry-run', action='store_true', default=False,
        help='Do not make changes.')
    parser.add_argument(
        '-v', '--verbose', action='store_true', default=False,
        help='Increase verbosity.')
    subparsers = parser.add_subparsers(help='sub-command help', dest="command")
    parser_list = subparsers.add_parser(
        'list', help='list artifacts for a job build')
    add_build_job_glob(parser_list)
    parser_get = subparsers.add_parser(
        'get', help='get artifacts for a job build')
    add_build_job_glob(parser_get)
    parser_get.add_argument(
        '-a', '--archive', action='store_true', default=False,
        help='Ensure the download path exists and remove older files.')
    parser_get.add_argument(
        'path', nargs='?', default='.',
        help="The path to download the files to.")
    add_credential_args(parser_list)
    add_credential_args(parser_get)
    parser_workspace = subparsers.add_parser(
        'setup-workspace', help='Setup and clean a workspace for building.')
    parser_workspace.add_argument(
        '-e', '--clean-env', dest='clean_env', default=None,
        help='Ensure the env resources are freed or deleted.')
    parser_workspace.add_argument(
        'path', help="The path to the existing workspace directory.")
    parsed_args = parser.parse_args(args)
    credentials = get_credentials(parsed_args)
    return parsed_args, credentials


def get_credentials(args):
    if 'user' not in args:
        return None
    if None in (args.user, args.password):
        return None
    return Credentials(args.user, args.password)


class PackageNamer:

    @classmethod
    def factory(cls):
        return cls(get_deb_arch(), get_distro_information()['RELEASE'])

    def __init__(self, arch, distro_release):
        self.arch = arch
        self.distro_release = distro_release

    def get_release_package(self, version):
        return (
            'juju-core_{version}-0ubuntu1~{distro_release}.1~juju1_{arch}.deb'
            ).format(version=version, distro_release=self.distro_release,
                     arch=self.arch)

    def get_certification_package(self, version):
        return (
            'juju-core_{version}.{distro_release}.1_{arch}.deb'
            ).format(version=version, distro_release=self.distro_release,
                     arch=self.arch)


def main(argv):
    """Manage list and get files from Juju CI builds."""
    args, credentials = parse_args(argv)
    try:
        if args.command == 'list':
            list_artifacts(
                credentials, args.job, args.build, args.glob,
                verbose=args.verbose)
        elif args.command == 'get':
            get_artifacts(
                credentials, args.job, args.build, args.glob, args.path,
                archive=args.archive, dry_run=args.dry_run,
                verbose=args.verbose)
        elif args.command == 'setup-workspace':
            setup_workspace(
                args.path, env=args.clean_env,
                dry_run=args.dry_run, verbose=args.verbose)
    except Exception as e:
        print(e)
        if args.verbose:
            traceback.print_tb(sys.exc_info()[2])
        return 2
    if args.verbose:
        print("Done.")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
