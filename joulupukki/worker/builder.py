from threading import Thread
import glob
import os
import shutil

import pecan
import git
import yaml

from joulupukki.lib.rpmpacker import RpmPacker
from joulupukki.lib.debpacker import DebPacker
from joulupukki.lib.distros import supported_distros, distro_templates
from joulupukki.lib.logger import get_logger, get_logger_docker
import json



from docker import Client
import re

import time

import urlparse

"""
scheduled
clonning
dispatching
finished
"""

class Builder(Thread):
    def __init__(self, data):
        Thread.__init__(self, name=data.uuid)
        self.source_url = data.source_url
        self.source_type = data.source_type
        self.branch = data.branch
        self.commit = data.commit
        self.uuid = data.uuid
        self.created = time.time()
        self.package_name = None
        self.package_version = None
        self.package_release = None

        # Create docker client
        self.cli = Client(base_url='unix://var/run/docker.sock', version="1.15")
        # Set folders
        self.folder = os.path.join(pecan.conf.builds_path, self.uuid)
        self.folder_source = os.path.join(self.folder, 'sources')
        os.makedirs(self.folder)
        # Prepare logger
        self.logger = get_logger(self.uuid)
        # Save
        self.save_to_disk()

    def save_to_disk(self):
        build_file = os.path.join(self.folder, "build.cfg")
        data = json.dumps({"source_url": self.source_url,
                           "source_type": self.source_type,
                           "branch": self.branch,
                           "commit": self.commit,
                           "uuid": self.uuid,
                           "created": self.created,
                           "package_name": self.package_name,
                           "package_version": self.package_version,
                           "package_release": self.package_release,
                           })

        with open(build_file, 'w') as f:
            f.write(data)

    def set_status(self, status, distro=None):
        if distro is None:
            status_file = os.path.join(self.folder, "status.txt")
        else:
            status_file = os.path.join(self.folder, distro, "status.txt")
        with open(status_file, 'w') as f:
            f.write(str(status))

    def git_clone(self):
        self.logger.info("Cloning")
        # Reworking url to handle password
        url_splitted = urlparse.urlsplit(self.source_url)
        username = url_splitted.username
        if username is None:
            username = 'anonymous'
        password = url_splitted.username
        if password is None:
            password = 'anonymous'
        url_data = [d for d in list(url_splitted)]
        new_netloc = username + ":" + password + "@" + url_data[1]
        url_data[1] = new_netloc
        source_url = urlparse.urlunsplit(url_data)
        # Clone repo
        try:
            repo = git.Repo.clone_from(source_url, self.folder_source)
        except Exception as exp:
            self.logger.error("Clonning error: %s", exp)
            return False
        branch_found = True
        # Get branch/tag if set
        if self.branch is not None:
            branch_found = False
            for ref in repo.refs:
                if ref.name == self.branch:
                    repo.head.reference = repo.commit(ref)
                    branch_found = True
                if ref.name == "origin/" + self.branch:
                    repo.head.reference = repo.commit(ref)
                    branch_found = True
        if branch_found is False:
            self.logger.error("Branch %s not found", self.branch)
            return False
        # Get commit if set
        if self.commit is not None:
            try:
                repo.head.reference = repo.commit(self.commit)
            except Exception as exp:
                self.logger.error("Bad commit: %s - %s", self.commit, exp)
                return False
        # Build tree
        repo.head.reset(index=True, working_tree=True)
        self.logger.info("Cloned")
        return True

    def get_sources(self):
        if self.source_type == 'git':
            return self.git_clone()
        elif self.source_type == 'local':
            url_splitted = urlparse.urlsplit(self.source_url)
            source_path = url_splitted.path
            if not os.path.isdir(source_path):
                self.logger.error("Source folder %s does not exist", source_path)
                return False
            shutil.copytree(source_path, self.folder_source, symlinks=True)
            return True
        else:
            self.logger.error("Source type %s not supported", self.source_type)
            return False
        return False

    def run_packer(self, packer_conf, root_folder):
        # DOCKER
        failed = False
        self.set_status('dispatching')
        for distro_name, build_conf in packer_conf.items():
            # Check yml format
            if not isinstance(build_conf, dict):
                self.logger.error("Packer yml file seems malformated" )
                self.set_status('bad_yml_file')
                continue
            # Check distro name
            if distro_name not in supported_distros:
                self.logger.error("Distro %s not supported", distro_name)
                self.set_status('distro_not_supported', distro_name)
                continue
            distro_type = distro_templates.get(distro_name)
            # Prepare distro configuration
            build_conf['distro'] = supported_distros.get(distro_name)
            build_conf['branch'] = self.branch
            build_conf['root_folder'] = root_folder
            # Launcher build
            self.logger.info("Distro %s is an %s distro", distro_name, distro_type)
            packer_class = globals().get(distro_type.capitalize() + 'Packer')
            packer = packer_class(self, build_conf)
            self.set_status('building', build_conf['distro'])
            self.logger.info("Packaging starting for %s", distro_name)
            if packer.run() is True:
                self.set_status('succeeded', build_conf['distro'])
                self.logger.info("Packaging finished for %s", distro_name)
            else:
                self.set_status('failed', build_conf['distro'])
                self.logger.info("Packaging finished for %s", distro_name)
        if failed:
            return False
        self.logger.info("Packaging finished for all distros")

    def run(self):
        # GIT
        self.logger.info("Started")
        self.set_status('clonning')
        if self.get_sources() is True:
            # YAML
            self.logger.debug("Read .packer.yml")
            self.set_status('reading')
            global_packer_conf_file_name = os.path.join(self.folder_source, ".packer.yml")
            if os.path.exists(global_packer_conf_file_name):
                global_packer_conf_stream = file(global_packer_conf_file_name, 'r')
                global_packer_conf = yaml.load(global_packer_conf_stream)
                if 'include' in global_packer_conf:
                    for packer_file_glob in global_packer_conf.get("include"):
                        for packer_conf_file_name in glob.glob(os.path.join(self.folder_source, packer_file_glob)):
                            packer_conf_stream = file(packer_conf_file_name, 'r')
                            packer_conf = yaml.load(packer_conf_stream)
                            # Get root folder of this package
                            packer_conf_relative_file_name = packer_conf_file_name.replace(self.folder_source, "").strip("/")
                            root_folder = os.path.dirname(packer_conf_relative_file_name)
                            # Run packer
                            self.run_packer(packer_conf, root_folder)
                else:
                     self.run_packer(global_packer_conf, ".")
            else:
                self.logger.error("File .packer.yml not found")
                self.set_status('failed')
        else:
            self.set_status('failed')

        # Delete tmp source folder
        self.logger.info("Tmp folders deleting")
        if os.path.exists(os.path.join(self.folder,'tmp')):
            shutil.rmtree(os.path.join(self.folder,'tmp'))
        for tmp_dir in glob.glob(os.path.join(self.folder, '*/tmp')):
            shutil.rmtree(tmp_dir)
        if os.path.exists(self.folder_source):
            shutil.rmtree(self.folder_source)
        self.logger.info("Tmp folders deleted")
