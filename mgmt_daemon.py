#!/usr/bin/env python
# encoding: utf-8
'''
AFL Management Daemon -- Tool to manage AFL queue and results

@author:     Christian Holler (:decoder)

@license:

This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.

@contact:    choller@mozilla.com
'''

# Ensure print() compatibility with Python 3
from __future__ import print_function

import sys
import os
import argparse
import time
import hashlib
import platform
import subprocess
import stat
import shutil
from boto.s3.connection import S3Connection
from boto.s3.key import Key

from FTB.ProgramConfiguration import ProgramConfiguration
from FTB.Running.AutoRunner import AutoRunner
from Collector.Collector import Collector

def get_machine_id(base_dir):
    '''
    Get (and if necessary generate) the machine id which is based on
    the current timestamp and the hostname of the machine. The
    generated ID is cached inside the AFL base directory, so all
    future calls to this method return the same ID.
    
    @type base_dir: String
    @param base_dir: AFL base directory
    
    @rtype: String
    @return: The generated/cached machine ID
    '''
    id_file = os.path.join(base_dir, "s3_id")
    
    # We initially create a unique ID based on the hostname and the
    # current timestamp, then we store this ID in a file inside the
    # fuzzing working directory so we can retrieve it later.
    if not os.path.exists(id_file):
        h = hashlib.new('sha1')
        h.update(platform.node())
        h.update(str(time.time()))
        id = h.hexdigest()
        with open(id_file, 'w') as id_fd:
            id_fd.write(id)
        return id
    else:
        with open(id_file, 'r') as id_fd:
            return id_fd.read()

def scan_crashes(base_dir):
    '''
    Scan the base directory for crash tests and submit them to FuzzManager.
    
    @type base_dir: String
    @param base_dir: AFL base directory
    
    @rtype: int
    @return: Non-zero return code on failure
    '''
    crash_dir = os.path.join(base_dir, "crashes")
    crash_files = []

    for crash_file in os.listdir(crash_dir):
        # Ignore all files that aren't crash results
        if not crash_file.startswith("id:"):
            continue
        
        crash_file = os.path.join(crash_dir, crash_file)

        # Ignore our own status files
        if crash_file.endswith(".submitted") or crash_file.endswith(".failed"):
            continue
        
        # Ignore files we already processed
        if os.path.exists(crash_file + ".submitted") or os.path.exists(crash_file + ".failed"):
            continue
        
        crash_files.append(crash_file)
        
    if crash_files:
        # First try to read necessary information for reproducing crashes
        cmdline = []
        test_idx = None
        with open(os.path.join(base_dir, "cmdline"), 'r') as cmdline_file:
            idx = 0
            for line in cmdline_file:
                if '@@' in cmdline:
                    test_idx = idx
                cmdline.append(line.rstrip('\n'))
                idx += 1
            
        if test_idx != None:
            orig_test_arg = cmdline[test_idx]

        print(cmdline)
        
        configuration = ProgramConfiguration.fromBinary(cmdline[0])
        if not configuration:
            print("Error: Creating program configuration from binary failed. Check your binary configuration file.", file=sys.stderr)
            return 2
        
        collector = Collector()
        
        for crash_file in crash_files:
            stdin = None
            
            if test_idx != None:
                cmdline[test_idx] = orig_test_arg.replace('@@', crash_file)
            else:
                with open(crash_file, 'r') as crash_fd:
                    stdin = crash_fd.read()
            
            runner = AutoRunner.fromBinaryArgs(cmdline[0], cmdline[1:], stdin=stdin)
            if runner.run():
                crash_info = runner.getCrashInfo(configuration)
                collector.submit(crash_info, crash_file)
                open(crash_file + ".submitted", 'a').close()
            else:
                open(crash_file + ".failed", 'a').close()
                print("Error: Failed to reproduce the given crash, cannot submit.", file=sys.stderr)

def upload_queue_dir(base_dir, bucket_name, project_name):
    '''
    Synchronize the queue directory of the specified AFL base directory
    to the specified S3 bucket. This method only uploads files that don't
    exist yet on the receiving side. 
    
    @type base_dir: String
    @param base_dir: AFL base directory
    
    @type bucket_name: String
    @param bucket_name: Name of the S3 bucket to use
    
    @type project_name: String
    @param project_name: Name of the project folder inside the S3 bucket
    '''
    queue_dir = os.path.join(base_dir, "queue")
    queue_files = []

    for queue_file in os.listdir(queue_dir):
        # Ignore all files that aren't crash results
        if not queue_file.startswith("id:"):
            continue
        
        queue_files.append(queue_file)

    cmdline_file = os.path.join(base_dir, "cmdline")
    
    conn = S3Connection()
    bucket = conn.get_bucket(bucket_name)
    
    remote_path = "%s/queues/%s/" % (project_name, get_machine_id(base_dir))
    
    remote_files = [key.name.replace(remote_path, "", 1) for key in list(bucket.list(remote_path))]
    
    upload_list = []
    
    for queue_file in queue_files:
        if not queue_file in remote_files:
            upload_list.append(os.path.join(queue_dir, queue_file))
    
    if not "cmdline" in remote_files:
        upload_list.append(cmdline_file)
    
    for upload_file in upload_list:
        remote_key = Key(bucket)
        remote_key.name = remote_path + os.path.basename(upload_file)
        print("Uploading file %s -> %s" % (upload_file, remote_key.name))
        remote_key.set_contents_from_filename(upload_file)
        
def download_queue_dirs(work_dir, bucket_name, project_name):
    '''
    Downloads all queue files into the queues sub directory of the specified
    local work directory. The files are renamed to match their SHA1 hashes
    to avoid file collisions.
    
    @type base_dir: String
    @param base_dir: Local work directory
    
    @type bucket_name: String
    @param bucket_name: Name of the S3 bucket to use
    
    @type project_name: String
    @param project_name: Name of the project folder inside the S3 bucket
    '''
    download_dir = os.path.join(work_dir, "queues")
    
    if not os.path.exists(download_dir):
        os.mkdir(download_dir)
    
    conn = S3Connection()
    bucket = conn.get_bucket(bucket_name)
    
    remote_path = "%s/queues/" % project_name
        
    remote_keys = list(bucket.list(remote_path))
    
    for remote_key in remote_keys:
        # If we see a cmdline file, fetch it into the main work directory
        if os.path.basename(remote_key.name) == 'cmdline':
            remote_key.get_contents_to_filename(os.path.join(work_dir, 'cmdline'))
            continue
        
        tmp_file = os.path.join(download_dir, "tmp")
        
        remote_key.get_contents_to_filename(tmp_file)
        
        with open(tmp_file, 'r') as tmp_fd:
            h = hashlib.new('sha1')
            h.update(str(tmp_fd.read()))
            hash_name = h.hexdigest()
        
        os.rename(tmp_file, os.path.join(download_dir, hash_name))
        
def download_build(build_dir, bucket_name, project_name):
    '''
    Downloads build.zip from the specified S3 bucket and unpacks it
    into the specified build directory.
    
    @type base_dir: String
    @param base_dir: Build directory
    
    @type bucket_name: String
    @param bucket_name: Name of the S3 bucket to use
    
    @type project_name: String
    @param project_name: Name of the project folder inside the S3 bucket
    '''
    
    # Clear any previous builds
    if os.path.exists(build_dir):
        shutil.rmtree(build_dir)
    
    os.mkdir(build_dir)
    
    zip_dest = os.path.join(build_dir, "build.zip")
    
    conn = S3Connection()
    bucket = conn.get_bucket(bucket_name)
    
    remote_key = Key(bucket)
    remote_key.name = "%s/build.zip" % project_name
    remote_key.get_contents_to_filename(zip_dest)
    
    subprocess.check_call(["unzip", zip_dest, "-d", build_dir])

def download_corpus(corpus_dir, bucket_name, project_name):
    '''
    Downloads the test corpus from the specified S3 bucket and project
    into the specified directory, without overwriting any files.
    
    @type corpus_dir: String
    @param corpus_dir: Directory where to store test corpus files
    
    @type bucket_name: String
    @param bucket_name: Name of the S3 bucket to use
    
    @type project_name: String
    @param project_name: Name of the project folder inside the S3 bucket
    '''
    if not os.path.exists(corpus_dir): 
        os.mkdir(corpus_dir)
    
    conn = S3Connection()
    bucket = conn.get_bucket(bucket_name)
    
    remote_path = "%s/corpus/" % project_name
        
    remote_keys = list(bucket.list(remote_path))
    
    for remote_key in remote_keys:
        dest_file = os.path.join(corpus_dir, os.path.basename(remote_key.name))
        
        if not os.path.exists(dest_file):
            remote_key.get_contents_to_filename(dest_file)

def upload_corpus(corpus_dir, bucket_name, project_name):
    '''
    Synchronize the specified test corpus directory to the specified S3 bucket. 
    This method only uploads files that don't exist yet on the receiving side. 
    
    @type corpus_dir: String
    @param corpus_dir: Directory where the test corpus files are stored
    
    @type bucket_name: String
    @param bucket_name: Name of the S3 bucket to use
    
    @type project_name: String
    @param project_name: Name of the project folder inside the S3 bucket
    '''
    test_files = os.listdir(corpus_dir)
    
    conn = S3Connection()
    bucket = conn.get_bucket(bucket_name)
    
    remote_path = "%s/corpus/" % project_name
    
    remote_files = [key.name.replace(remote_path, "", 1) for key in list(bucket.list(remote_path))]
    
    upload_list = []
    
    for test_file in test_files:
        if not test_file in remote_files:
            upload_list.append(os.path.join(corpus_dir, test_file))
    
    for upload_file in upload_list:
        remote_key = Key(bucket)
        remote_key.name = remote_path + os.path.basename(upload_file)
        print("Uploading file %s -> %s" % (upload_file, remote_key.name))
        remote_key.set_contents_from_filename(upload_file)


def main(argv=None):
    '''Command line options.'''

    program_name = os.path.basename(sys.argv[0])

    if argv is None:
        argv = sys.argv[1:]

    # setup argparser
    parser = argparse.ArgumentParser()

    parser.add_argument("--s3-queue-upload", dest="s3_queue_upload", action='store_true', help="Use S3 to synchronize queues")
    parser.add_argument("--s3-build-download", dest="s3_build_download", help="Use S3 to download the build for the specified project", metavar="DIR")
    parser.add_argument("--s3-corpus-download", dest="s3_corpus_download", help="Use S3 to download the test corpus for the specified project", metavar="DIR")
    parser.add_argument("--s3-corpus-refresh", dest="s3_corpus_refresh", help="Download queues and corpus from S3, combine and minimize, then re-upload.", metavar="DIR")
    parser.add_argument("--fuzzmanager", dest="fuzzmanager", action='store_true', help="Use FuzzManager to submit crash results")
    parser.add_argument("--afl-output-dir", dest="afloutdir", help="Path to the AFL output directory to manage", metavar="DIR")
    parser.add_argument("--afl-binary-dir", dest="aflbindir", help="Path to the AFL binary directory to use", metavar="DIR")
    
    parser.add_argument("--s3-bucket", dest="s3_bucket", help="Name of the S3 bucket to use", metavar="NAME")
    parser.add_argument("--project", dest="project", help="Name of the subfolder/project inside the S3 bucket", metavar="NAME")

    parser.add_argument('rargs', nargs=argparse.REMAINDER)

    if len(argv) == 0:
        parser.print_help()
        return 2

    # process options
    opts = parser.parse_args(argv)
    
    # Upload and FuzzManager modes require specifying the AFL directory
    if opts.s3_queue_upload or opts.fuzzmanager:
        if not opts.afldir:
            print("Error: Must specify AFL output directory using --afl-output-dir", file=sys.stderr)
            return 2
        
    if opts.s3_queue_upload or opts.s3_corpus_refresh or opts.s3_build_download or opts.s3_corpus_download:
        if not opts.s3_bucket or not opts.project:
            print("Error: Must specify both --s3-bucket and --project for S3 actions", file=sys.stderr)
            return 2
        
    if opts.s3_build_download:
        download_build(opts.s3_build_download, opts.s3_bucket, opts.project)
        return 0
    
    if opts.s3_corpus_download:
        download_corpus(opts.s3_corpus_download, opts.s3_bucket, opts.project)
        return 0

    if opts.s3_corpus_refresh:
        if not opts.aflbindir:
            print("Error: Must specify --afl-binary-dir for refreshing the test corpus", file=sys.stderr)
            return 2
        
        if not os.path.exists(opts.s3_corpus_refresh):
            os.makedirs(opts.s3_corpus_refresh)
            
        queues_dir = os.path.join(opts.s3_corpus_refresh, "queues")
        
        print("Downloading AFL queues from s3://%s/%s/queues/ to %s" % (opts.s3_bucket, opts.project, queues_dir)) 
        download_queue_dirs(opts.s3_corpus_refresh, opts.s3_bucket, opts.project)
        
        cmdline_file = os.path.join(opts.s3_corpus_refresh, "cmdline")
        if not os.path.exists(cmdline_file):
            print("Error: Failed to download a cmdline file from queue directories.", file=sys.stderr)
            return 2
        
        download_build(os.path.join(opts.s3_corpus_refresh, "build"), opts.s3_bucket, opts.project)
        
        cmdline = []
        with open(os.path.join(opts.s3_corpus_refresh, "cmdline"), 'r') as cmdline_file:
            for line in cmdline_file:
                cmdline.append(line.rstrip('\n'))
                
        # Assume cmdline[0] is the name of the binary
        binary_name = os.path.basename(cmdline[0])
        
        # Try locating our binary in the build we just unpacked
        binary_search_result = [os.path.join(dirpath, filename) 
            for dirpath, dirnames, filenames in os.walk(os.path.join(opts.s3_corpus_refresh, "build")) 
                for filename in filenames 
                    if (filename == binary_name and (stat.S_IXUSR & os.stat(os.path.join(dirpath, filename))[stat.ST_MODE]))]
        
        if not binary_search_result:
            print("Error: Failed to locate binary %s in unpacked build." % binary_name, file=sys.stderr)
            return 2
        
        if len(binary_search_result) > 1:
            print("Error: Binary name %s is ambiguous in unpacked build." % binary_name, file=sys.stderr)
            return 2
        
        cmdline[0] = binary_search_result[0]
        
        # Download our current corpus into the queues directory as well
        download_corpus(queues_dir, opts.s3_bucket, opts.project)
        
        # Ensure the directory for our new tests is empty
        updated_tests_dir = os.path.join(opts.s3_corpus_refresh, "tests")
        if os.path.exists(updated_tests_dir):
            shutil.rmtree(updated_tests_dir)
        os.mkdir(updated_tests_dir)
        
        # Run afl-cmin
        afl_cmin = os.path.join(opts.aflbindir, "afl-cmin")
        if not os.path.exists(afl_cmin):
            print("Error: Unable to locate afl-cmin binary.", file=sys.stderr)
            return 2
        
        afl_cmdline = [afl_cmin, '-e', '-i', queues_dir, '-o', updated_tests_dir, '-t', '1000', '-m', '-1']
        afl_cmdline.extend(cmdline)
        
        subprocess.check_call(afl_cmdline, env={ 'LD_LIBRARY_PATH' : os.path.dirname(cmdline[0]) })
        
        upload_corpus(updated_tests_dir, opts.s3_bucket, opts.project)
        
    
    if opts.fuzzmanager or opts.s3_queue_upload:
        while True:
            if opts.fuzzmanager:
                scan_crashes(opts.afloutdir)
                
            if opts.s3_queue_upload:
                upload_queue_dir(opts.afldir, opts.s3_bucket, opts.project)
                
        time.sleep(10)



if __name__ == "__main__":
    sys.exit(main())
