import argparse
import shutil
import sys

from .. import utils
from ..foundation.command.Command import Command
from ..manager.ManagerProxy import ManagerProxy
from ..setting.Setting import Setting


class WipeCommand(Command):
    __slots__ = ['parser']

    def __init__(self):
        Command.__init__(self)

        parser = argparse.ArgumentParser(
            prog='kathara wipe',
            description='Delete all Kathara machines and links, optionally also delete settings.'
        )

        parser.add_argument(
            '-f', '--force',
            required=False,
            action='store_true',
            help='Force the wipe.'
        )

        parser.add_argument(
            '-s', '--settings',
            required=False,
            action='store_true',
            help='Wipe Kathara and all the stored settings.'
        )

        parser.add_argument(
            '-a', '--all',
            required=False,
            action='store_true',
            help='Wipe Kathara machines and links of all users.'
        )

        self.parser = parser

    def run(self, current_path, argv):
        args = self.parser.parse_args(argv)

        if not args.force:
            utils.confirmation_prompt("Are you sure to wipe Kathara?", lambda: None, sys.exit)

        ManagerProxy.get_instance().wipe(all_users=bool(args.all))

        if args.settings:
            Setting.wipe()
        else:
            setting_object = Setting.get_instance()
            setting_object.net_counter = 0
            setting_object.save_selected(['net_counter'])

        vlab_dir = utils.get_vlab_temp_path(force_creation=False)
        shutil.rmtree(vlab_dir, ignore_errors=True)
