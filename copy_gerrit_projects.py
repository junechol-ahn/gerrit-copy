#!/usr/bin/env python3

import sys
import os
import subprocess
import logging
import datetime
import re
import argparse
import shutil

# Create the log directory if it doesn't exist
LOG_DIR="./log"
os.makedirs(LOG_DIR, exist_ok=True)

# Create the log directory if it doesn't exist
DOWNLOAD_DIR="./download"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# create logger with 'clone_gerrit'
logger = logging.getLogger('clone_gerrit')
logfilename=f'{LOG_DIR}/{datetime.datetime.now().strftime("%Y%m%d-%H%M%S")}.log'
logging.basicConfig(format='%(asctime)s line:%(lineno)3d %(levelname)-8s %(message)s', 
                    datefmt='%Y-%m-%d %H:%M:%S',
                    level=logging.DEBUG, 
                    handlers=[
                        logging.FileHandler(logfilename),
                        logging.StreamHandler()
                    ])

def print_result(result):
    cmd = f"{' '.join(result.args)}"
    if result.returncode:
        logging.error(f"!! Err: '{cmd}' returned {result.returncode}")
        logging.error("")
    else:
        logging.debug(f">> Exec: {cmd}")
        logging.debug("")

def parse_projects_file(filename, delete_local):
    """
    Parse projects file and print source -> destination mapping
    """
    if not os.path.exists(filename):
        logging.error(f"Error: File '{filename}' not found")
        return
    
    try:
        # Read all lines
        with open(filename, 'r') as file:
            lines = file.readlines()

        # Parse configuration at the beginning
        config = {}
        config_lines = 0

        # Process lines until we find a non-config line
        for i, line in enumerate(lines):
            if line.strip() and not line.strip().startswith('#'):
                # Check if this line is a configuration
                match = re.match(r'^(\w+)\s*=\s*(.+)$', line.strip())
                if match:
                    key, value = match.groups()
                    config[key] = value
                    config_lines += 1
                else:
                    # This is the first non-config line, break
                    break

        # Extract configuration values
        src_ip = config.get('SRC_IP', '10.166.232.51')
        src_ssh_port = config.get('SRC_SSH_PORT', '29418')
        src_username = config.get('SRC_USERNAME', 'cicd.slsi')
        src_server = f'ssh://{src_username}@{src_ip}:{src_ssh_port}'
        dst_ip = config.get('DST_IP', 'localhost')
        dst_ssh_port = config.get('DST_SSH_PORT', '29418')
        dst_username = config.get('DST_USERNAME', 'admin')
        
        logging.info(f"{src_server=}")
        logging.info(f"{dst_ip=}")
        logging.info(f"{dst_ssh_port=}")
        logging.info(f"{dst_username=}")

        # Process projects from the remaining lines
        total_projects = 0
        current_project = 0

        # change directory to DOWNLOAD_DIR to download the sourc
        os.chdir(DOWNLOAD_DIR)
        logging.info(f"Current working directory: {os.getcwd()}")


        for line_num, line in enumerate(lines[config_lines:]):
            # Strip whitespace and skip empty lines/comments
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            
            # Split line into parts
            parts = line.split()
            if not parts:
                continue
            
            # First part is always the source project name
            src_project = parts[0]
            
            # Second part is destination, if exists, otherwise use source
            if len(parts) >= 2:
                dst_project = parts[1]
            else:
                dst_project = src_project
            
            # Print the mapping
            logging.info("")
            logging.info("")
            logging.info(f"{src_project} ---> {dst_project}")

            # git clone source project
            logging.info(f"Cloning project {src_project} from source server")
            result = subprocess.run(["git", "clone", "--bare", f"{src_server}/{src_project}", src_project])
            print_result(result)


            # create project on destination
            logging.info(f"Creating project {dst_project} on destination server")
            result = subprocess.run(["ssh", "-p", dst_ssh_port, f"{dst_username}@{dst_ip}", "gerrit", "create-project", dst_project, "--signed-off-by", "FALSE"])
            print_result(result)


            logging.info(f"Changing remote config to push...")
            result = subprocess.run(["git", "remote", "set-url", "origin", 
                            f"ssh://{dst_username}@{dst_ip}:{dst_ssh_port}/{dst_project}"], cwd=f"./{src_project}")
            print_result(result)

            # push local project to destination
            logging.info(f"Push branches to remote...")
            result = subprocess.run(["git", "push", "-o", "skip-validation", "--all"], cwd=f"./{src_project}")
            print_result(result)
            logging.info(f"Push tags to remote...")
            result = subprocess.run(["git", "push", "-o", "skip-validation", "--tags"], cwd=f"./{src_project}")
            print_result(result)

            # (if delete_local) delete local project copy
            if delete_local:
                if os.path.exists(f"./{src_project}"):
                    try:
                        shutil.rmtree(f"./{src_project}")
                        logging.info(f"디렉토리 '{src_project}'와 모든 내용을 삭제했습니다.")
                    except OSError as e:
                        logging.error(f"디렉토리 삭제 오류: {e}")
                else:
                    logging.error(f"디렉토리 '{src_project}'가 존재하지 않습니다.")


    except Exception as e:
        logging.error(f"Error reading file: {e}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("projects_file", help="a file which has connection details and project list")
    parser.add_argument("-d", "--delete_local_download", help="delete downloaded project after each push to target", action="store_true")
    args = parser.parse_args()

    delete_local = args.delete_local_download
    if delete_local:
        logging.info(f"delete_local_download is ON. local download will be deleted.")
    else:
        logging.info(f"delete_local_download is OFF. local downloaded files are kept.")
    projects_file = args.projects_file
    parse_projects_file(projects_file, delete_local)

if __name__ == "__main__":
    main()
