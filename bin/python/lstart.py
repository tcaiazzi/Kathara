import argparse
import netkit_commons as nc 
import lstart_create as cr

DEBUG = nc.DEBUG
nc.DEBUG = False

parser = argparse.ArgumentParser(description='Create and start a Netkit Lab.')
parser.add_argument('path')

args = parser.parse_args()

# get lab machines, options, links and metadata
(machines, links, options, metadata) = nc.lab_parse(args.path)
# get command lists
(commands, startup_commands, exec_commands) = nc.create_commands(machines, links, options, metadata, args.path)

# create lab
cr.lab_create(commands, startup_commands)

# print commands for terminal
for exec_command in exec_commands:
    print("bash -c '" + exec_command + "'")