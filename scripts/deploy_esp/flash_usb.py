#!/usr/bin/env python3

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2021 Intel Corporation

# pylint: disable=too-many-lines

"""
This script flashes selected usb drive with appropriate bios image generated by dek_provision.py.

Target audience are end customers deploying Experience Kits.

Script was written for Python 3.6+ and uses no external dependencies, except for yaml parser.

Example usage scenario (assuming custom config file is used instead of existing default_config.yml):
User should provide default config or a generated one provided by dek_provisioning.py.

Available script options. Flash USB device:
 - with appropriate bios set in default config:     dek_flash.py --dev <usb_device_path>
 - with appropriate bios set in provided config:    dek_flash.py --dev <usb_device_path> --config <config_yaml_path>
 - with appropriate image specified by url:         dek_flash.py --dev <usb_device_path> --url <image_url>
"""

import argparse
import logging
import os
import pathlib
import signal
import subprocess  # nosec - bandit: security considered
import sys
import traceback
import urllib.request
import urllib.parse
import urllib.error
from socket import timeout

import seo.error
import seo.git
import seo.stage

try:
    import yaml
except ModuleNotFoundError:
    sys.stderr.write(
        "ERROR: Couldn't import yaml module.\n"
        "   It can be installed using following command:\n"
        "   $ pip3 install pyyaml\n")
    sys.exit(seo.error.Codes.MISSING_PREREQUISITE)


def parse_args(default_config_path, experience_kit_name):
    """ Parse script arguments """

    experience_kit_name = (
        "" if experience_kit_name is None else
        "{0:s} ".format(experience_kit_name))

    p = argparse.ArgumentParser(
        description=f"""Start the Smart Edge Open {experience_kit_name} USB flash process.""")
    p.add_argument(
        "-d", "--dev", action="store", dest="dev_path", metavar="PATH",
        help="Path to usb devices, for example '/dev/sdc'. WARNING: this will wipe out the target device. "
             "If omitted it will provide instructions how to flash a USB device. (default: %(default)s)")
    p.add_argument(
        "-u", "--url", action="store", dest="image_url", metavar="PATH",
        help="URL of the USB Image")
    p.add_argument(
        "-c", "--config", action="store", dest="config_file", metavar="PATH",
        default=default_config_path,
        help="Configuration file PATH for USB flash (default: %(default)s)")
    p.add_argument(
        "--debug", action="store_true", dest="debug",
        help="provide more verbose diagnostic information")

    return p.parse_args()


def get_config(config_file_path):
    """ Read and parse given provisioning config file """

    logging.debug("Trying to read and parse provisioning configuration file ('%s')", config_file_path)

    try:
        raw_config = pathlib.Path(config_file_path).read_text()
    except (FileNotFoundError, PermissionError) as e:
        raise seo.error.AppException(
            seo.error.Codes.ARGUMENT_ERROR,
            f"Failed to load the config file: {e}") from e

    try:
        return yaml.safe_load(raw_config)
    except yaml.YAMLError as e:
        raise seo.error.AppException(
            seo.error.Codes.CONFIG_ERROR,
            f"Config file format error: {e}") from e


def check_preconditions(args):
    """ Check script's preconditions """

    logging.debug("Checking preconditions")

    # check current user permissions
    if os.getuid() != 0:
        raise seo.error.AppException(
            seo.error.Codes.MISSING_PREREQUISITE,
            "This script must be run as the root user; Use the 'sudo su -' command to change it")

    if args.dev_path is None:
        raise seo.error.AppException(
            seo.error.Codes.MISSING_PREREQUISITE,
            "Device path not specified.")

    dev_path = pathlib.Path(args.dev_path)

    if not dev_path.exists():
        raise seo.error.AppException(
            seo.error.Codes.MISSING_PREREQUISITE,
            f"Device path does not exist in expected location '{dev_path}'")

    # check if image is accessible and is in the right format
    if args.image_url:
        parsed = urllib.parse.urlparse(args.image_url)
        url = parsed.geturl()

        if not url.lower().startswith('http'):
            raise seo.error.AppException(
                seo.error.Codes.ARGUMENT_ERROR,
                "URL format incorrect, should start with http:\n"
                f"    {url}")

        if not url.lower().endswith('.img'):
            raise seo.error.AppException(
                seo.error.Codes.ARGUMENT_ERROR,
                "URL is not pointing to an img file:\n"
                f"    {url}")

        if all(bios_type not in url for bios_type in ("-bios", "-efi")):
            raise seo.error.AppException(
                seo.error.Codes.ARGUMENT_ERROR,
                "URL is not pointing to an acceptable file. "
                "Provided image should have a format of: {profile}-{bios}.img\n"
                f"    {url}")

        try:
            urllib.request.urlopen(url, timeout=1)  # nosec - bandit: security considered
        except (urllib.error.HTTPError, urllib.error.URLError, timeout) as e:
            raise seo.error.AppException(
                seo.error.Codes.ARGUMENT_ERROR,
                "Wrong USB image URL: \n"
                f"    {url}\n"
                f"    {e}")


def choose_profile(config):
    """ Choose OS profile """

    logging.debug("Choosing OS profile based on configuration file ('%s')", config)

    usb_config = config['usb_images']
    profiles = [profile['name'] for profile in config['profiles']]

    if not usb_config['all_in_one']:
        logging.info("All-in-one image generation set to false in config.\n")

        for idx, profile in enumerate(profiles):
            print(f"{idx + 1}. {profile}\n")

        try:
            profile_choice = int(input("Please choose which profile to flash: "))
        except ValueError as e:
            raise seo.error.AppException(
                seo.error.Codes.ARGUMENT_ERROR,
                "Wrong profile number:\n"
                f"    {e}")

        if profile_choice < 1 or profile_choice > len(profiles):
            raise seo.error.AppException(
                seo.error.Codes.ARGUMENT_ERROR,
                f"Wrong profile number: {profile_choice}")

        profile = profiles[profile_choice - 1]

    else:
        profile = "all"
        logging.info("All-in-one image generation set to true in config. Using all-in-one image for specified bios.")

    return profile


def choose_bios(config):
    """ Choose BIOS """

    logging.debug("Choosing BIOS based on configuration file ('%s')", config)

    usb_config = config['usb_images']

    if not usb_config['build']:
        raise seo.error.AppException(
            seo.error.Codes.CONFIG_ERROR,
            "Build parameter set to false in config.")

    if usb_config['bios'] and usb_config['efi']:
        bios_type = input("Which bios type image do you want write to USB? ('bios' or 'efi'): ")

        if bios_type not in ("bios", "efi"):
            raise seo.error.AppException(
                seo.error.Codes.ARGUMENT_ERROR,
                "Wrong bios type specified:\n"
                f"    {bios_type}")

        bios = bios_type

    elif usb_config['bios']:
        bios = "bios"
    elif usb_config['efi']:
        bios = "efi"
    else:
        raise seo.error.AppException(
            seo.error.Codes.CONFIG_ERROR,
            "Couldn't recognize expected bios type. Both 'bios' and 'efi' set to false. Check config.")

    return bios


def generate_command(config, dev_path, profile, bios):
    """ Generate command for flashing USB.
        Provided image should have a format of: {profile}-{bios}.img
    """

    workdir = pathlib.Path(config['usb_images']['output_path'])

    image_path = workdir / f"{profile}-{bios}.img"

    if not image_path.exists():
        raise seo.error.AppException(
            seo.error.Codes.RUNTIME_ERROR,
            "Installation image couldn't be found in expected location:\n"
            f"    {image_path}")

    return [
        './flashusb.sh',
        '-d',
        str(dev_path),
        '-i',
        f"../{str(image_path)}",
        '-b',
        str(bios),
    ]


def run_flash_usb_script(config, cmd):
    """ Helper to run ESP flashusb.sh script """

    if isinstance(cmd, list):
        cmd = " ".join(cmd)

    # convert to Path if string was passed
    workdir = pathlib.Path(config['esp']['dest_dir'])
    if not workdir.exists():
        raise seo.error.AppException(
            seo.error.Codes.CONFIG_ERROR,
            f"Workdir does not exist in expected location '{workdir}', required by '{cmd}'")

    logging.debug("Running command: %s", cmd)
    with subprocess.Popen(cmd, shell=True, cwd=workdir) as proc: # nosec - B602
        try:
            while proc.poll() is None:
                if proc.stdout is None:
                    continue

                if line := proc.stdout.readline():
                    # newlines are already contained in the output
                    print(line, end='')

            if proc.poll() != 0:
                raise RuntimeError(
                    f"Running ESP script failed. Inspect output and logs in {workdir}/builder.log")
        except KeyboardInterrupt as e:
            # gracefully handle SIGINT, kill script that may be stuck in background.
            # note: since script can start bunch of other scripts, it sometimes is not killed with simple
            # proc.terminate() or proc.kill(), therefore we need to kill whole process group.
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            raise RuntimeError("Interrupted by user") from e


# ---------------------------------------------


def run_main(default_config_path=None, experience_kit_name=""):
    """ Top level script entry function """

    default_config_path = os.path.relpath(
        os.path.join(
            os.path.dirname(os.path.realpath(__file__)),
            "default_config.yml")) if default_config_path is None else default_config_path

    args = parse_args(default_config_path, experience_kit_name)

    try:
        sys.exit(main(args).value)
    except seo.error.AppException as e:
        if args.debug:
            traceback.print_exc(file=sys.stderr)
        logging.error(e.code if e.msg is None else e.msg)
        sys.exit(e.code.value)


def main(args):
    """ Internal main function """

    check_preconditions(args)

    if args.debug:
        log_level = logging.DEBUG
        log_format = '%(asctime)s.%(msecs)03d %(levelname)s: %(message)s'
    else:
        log_level = logging.INFO
        log_format = '%(levelname)s: %(message)s'

    logging.basicConfig(level=log_level, format=log_format, datefmt='%Y-%m-%d %H:%M:%S')

    cfg = get_config(args.config_file)
    dev_path = pathlib.Path(args.dev_path)
    if url := args.image_url:
        cmd = ['./flashusb.sh', '-d', str(dev_path), '-u', str(url)]

    else:
        profile = choose_profile(cfg)
        bios = choose_bios(cfg)
        cmd = generate_command(cfg, dev_path, profile, bios)
    run_flash_usb_script(cfg, cmd)

    return seo.error.Codes.NO_ERROR


if __name__ == "__main__":
    run_main()
