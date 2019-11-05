import logging
from itertools import islice
from subprocess import Popen

from docker.errors import APIError

from ... import utils
from ...exceptions import MountDeniedError, MachineAlreadyExistsError
from ...setting.Setting import Setting

RP_FILTER_NAMESPACE = "net.ipv4.conf.%s.rp_filter"
SYSCTL_COMMAND = "sysctl %s=0" % RP_FILTER_NAMESPACE

# Known commands that each container should execute
# Run order: rp_filters, shared.startup, machine.startup and machine.startup_commands
STARTUP_COMMANDS = [
    # Copy the machine folder (if present) from the hostlab directory into the root folder of the container
    # In this way, files are all replaced in the container root folder
    # rsync is used to keep symlinks while copying files.
    "if [ -d \"/hostlab/{machine_name}\" ]; then "
    "rsync -r -K /hostlab/{machine_name}/* /; fi",

    # Patch the /etc/resolv.conf file. If present, replace the content with the one of the machine.
    # If not, clear the content of the file.
    # This should be patched with "cat" because file is already in use by Docker internal DNS.
    "if [ -f \"/hostlab/{machine_name}/etc/resolv.conf\" ]; then " \
    "cat /hostlab/{machine_name}/etc/resolv.conf > /etc/resolv.conf; else " \
    "echo \"\" > /etc/resolv.conf; fi",

    # Give proper permissions to /var/www
    "chmod -R 777 /var/www/*",

    # Give proper permissions to Quagga files (if present)
    "if [ -d \"/etc/quagga\" ]; then "
    "chown quagga:quagga /etc/quagga/*",
    "chmod 640 /etc/quagga/*; fi",

    # If shared.startup file is present
    "if [ -f \"/hostlab/shared.startup\" ]; then "
    # Give execute permissions to the file and execute it
    # We redirect the output "&>" to a debugging file
    "chmod u+x /hostlab/shared.startup",
    # Adds a line to enable command output
    "sed -i \"1s;^;set -x\\n\\n;\" /hostlab/shared.startup",
    "/hostlab/shared.startup &> /var/log/shared.log; fi",

    # If .startup file is present
    "if [ -f \"/hostlab/{machine_name}.startup\" ]; then "
    # Give execute permissions to the file and execute it
    # We redirect the output "&>" to a debugging file
    "chmod u+x /hostlab/{machine_name}.startup",
    # Adds a line to enable command output
    "sed -i \"1s;^;set -x\\n\\n;\" /hostlab/{machine_name}.startup",
    "/hostlab/{machine_name}.startup &> /var/log/startup.log; fi",

    # Placeholder for user commands
    "{machine_commands}"
]

RP_FILTER_COMMANDS = [
    # Remount /proc/sys in rw to patch rp_filter value
    "mount -o remount,rw /proc/sys",

    # Placeholder for rp_filter patches
    "{rp_filter}",

    # Remount /proc/sys in ro after patching rp_filter value
    "mount -o remount,ro /proc/sys",
]

SHUTDOWN_COMMANDS = [
    # If machine.shutdown file is present
    "if [ -f \"/hostlab/{machine_name}.shutdown\" ]; then "
    # Give execute permissions to the file and execute it
    "chmod u+x /hostlab/{machine_name}.shutdown; /hostlab/{machine_name}.shutdown; fi",

    # If shared.shutdown file is present
    "if [ -f \"/hostlab/shared.shutdown\" ]; then "
    # Give execute permissions to the file and execute it
    "chmod u+x /hostlab/shared.shutdown; /hostlab/shared.shutdown; fi"
]


class DockerMachine(object):
    __slots__ = ['client', 'docker_image']

    def __init__(self, client, docker_image):
        self.client = client

        self.docker_image = docker_image

    def deploy(self, machine):
        logging.debug("Creating machine `%s`..." % machine.name)

        image = machine.get_image()
        memory = machine.get_mem()
        cpus = machine.get_cpu()
        ports = machine.get_ports()

        # Get the general options into a local variable (just to avoid accessing the lab object every time)
        options = machine.lab.general_options

        # If bridged is required in command line but not defined in machine meta, add it.
        if "bridged" in options and not machine.bridge:
            machine.add_meta("bridged", True)

        # If any exec command is passed in command line, add it.
        if "exec" in options:
            machine.add_meta("exec", options["exec"])

        # Get the first network object, if defined.
        # This should be used in container create function
        first_network = None
        if machine.interfaces:
            first_network = machine.interfaces[0].api_object

        # If no interfaces are declared in machine, but bridged mode is required, get bridge as first link.
        # Flag that bridged is already connected (because there's another check in `start`).
        if first_network is None and machine.bridge:
            first_network = machine.bridge.api_object
            machine.add_meta("bridge_connected", True)

        # Sysctl params to pass to the container creation
        sysctl_parameters = {RP_FILTER_NAMESPACE % x: 0 for x in ["all", "default", "lo"]}
        sysctl_parameters["net.ipv4.ip_forward"] = 1
        # TODO: ipv6_forward not found?
        # sysctl_parameters["net.ipv6.ip_forward"] = 1

        volumes = {}

        if Setting.get_instance().shared_mount and machine.lab.shared_folder:
            volumes = {machine.lab.shared_folder: {'bind': '/shared', 'mode': 'rw'}}

        # Mount the host home only if specified in settings.
        if Setting.get_instance().hosthome_mount:
            volumes[utils.get_current_user_home()] = {'bind': '/hosthome', 'mode': 'rw'}

        self.docker_image.check_and_pull(image)

        container_name = self.get_container_name(machine.name, machine.lab.folder_hash)
        try:
            machine_container = self.client.containers.create(image=image,
                                                              name=container_name,
                                                              hostname=machine.name,
                                                              cap_add=machine.capabilities,
                                                              network=first_network.name if first_network else None,
                                                              network_mode="bridge" if first_network else "none",
                                                              sysctls=sysctl_parameters,
                                                              mem_limit=memory,
                                                              nano_cpus=cpus,
                                                              ports=ports,
                                                              tty=True,
                                                              stdin_open=True,
                                                              detach=True,
                                                              volumes=volumes,
                                                              labels={"name": machine.name,
                                                                      "lab_hash": machine.lab.folder_hash,
                                                                      "user": utils.get_current_user_name(),
                                                                      "app": "kathara"
                                                                      }
                                                              )
        except APIError as e:
            if e.response.status_code == 409 and e.explanation.startswith('Conflict.'):
                raise MachineAlreadyExistsError("Machine with name `%s` already exists." % machine.name)
            else:
                raise e

        # Pack machine files into a tar.gz and extract its content inside `/`
        tar_data = machine.pack_data()
        if tar_data:
            machine_container.put_archive("/", tar_data)

        machine.api_object = machine_container

    def update(self, machine):
        machines = self.get_machines_by_filters(machine_name=machine.name, lab_hash=machine.lab.folder_hash)

        if not machines:
            raise Exception("Machine `%s` not found." % machine.name)

        machine.api_object = machines.pop()

        attached_networks = machine.api_object.attrs["NetworkSettings"]["Networks"]
        last_interface = len(attached_networks) - 1 if "none" in attached_networks else len(attached_networks)

        rp_filter_commands = []
        # Connect the container to its new networks
        for (_, machine_link) in machine.interfaces.items():
            machine_link.api_object.connect(machine.api_object)

            # Add the rp_filter patch for this interface
            rp_filter_commands.append(SYSCTL_COMMAND % ("eth%d" % last_interface))

            last_interface += 1

        # Execute the rp_filter patch for the new interfaces
        machine.api_object.exec_run(cmd=[Setting.get_instance().device_shell, '-c', "; ".join(rp_filter_commands)],
                                    stdout=False,
                                    stderr=False,
                                    privileged=True,
                                    detach=True
                                    )

    @staticmethod
    def start(machine):
        logging.debug("Starting machine `%s`..." % machine.name)

        try:
            machine.api_object.start()
        except APIError as e:
            if e.response.status_code == 500 and e.explanation.startswith('Mounts denied'):
                raise MountDeniedError("Host drive is not shared with Docker.")
            else:
                raise e

        # Build sysctl rp_filter patch for interfaces (since eth0 is already connected, we already put it here)
        rp_filter_commands = [SYSCTL_COMMAND % "eth0"]

        # Connect the container to its networks (starting from the second, the first is already connected in `create`)
        # This should be done after the container start because Docker causes a non-deterministic order when attaching
        # networks before container startup.
        for (iface_num, machine_link) in islice(machine.interfaces.items(), 1, None):
            logging.debug("Connecting machine `%s` to network `%s` on interface %d..." % (machine.name,
                                                                                          machine_link.name,
                                                                                          iface_num
                                                                                          )
                          )

            machine_link.api_object.connect(machine.api_object)

            # Add the rp_filter patch for this interface
            rp_filter_commands.append(SYSCTL_COMMAND % ("eth%d" % iface_num))

        # Create the rp_filter commands string
        rp_filter_commands_string = "; ".join(RP_FILTER_COMMANDS) \
                                        .format(rp_filter="; ".join(rp_filter_commands))

        # Execute the rp_filter commands inside the container (with privileged in order to use `mount`)
        machine.api_object.exec_run(cmd=[Setting.get_instance().device_shell, '-c', rp_filter_commands_string],
                                    stdout=False,
                                    stderr=False,
                                    privileged=True,
                                    detach=True
                                    )

        # Bridged connection required but not added in `deploy` method.
        if "bridge_connected" not in machine.meta and machine.bridge:
            machine.bridge.api_object.connect(machine.api_object)

        # Build the final startup commands string
        startup_commands_string = "; ".join(STARTUP_COMMANDS) \
                                      .format(machine_name=machine.name,
                                              machine_commands="; ".join(machine.startup_commands)
                                              )

        # Execute the startup commands inside the container (without privileged flag so basic permissions are used)
        machine.api_object.exec_run(cmd=[Setting.get_instance().device_shell, '-c', startup_commands_string],
                                    stdout=False,
                                    stderr=False,
                                    privileged=False,
                                    detach=True
                                    )

        if Setting.get_instance().open_terminals:
            machine.connect(Setting.get_instance().terminal)

    def undeploy(self, lab_hash, selected_machines=None):
        machines = self.get_machines_by_filters(lab_hash=lab_hash)

        for machine in machines:
            # If selected machines list is empty, remove everything
            # Else, check if the machine is in the list.
            if not selected_machines or \
               machine.labels["name"] in selected_machines:
                logging.info("Deleting machine %s." % machine.labels["name"])

                self.delete_machine(machine)

    def wipe(self, user=None):
        machines = self.get_machines_by_filters(user=user)

        for machine in machines:
            self.delete_machine(machine)

    def connect(self, lab_hash, machine_name, shell, logs=False):
        logging.debug("Connect to machine with name: %s" % machine_name)

        containers = self.get_machines_by_filters(lab_hash=lab_hash, machine_name=machine_name)

        logging.debug("Found containers: %s" % str(containers))

        if len(containers) != 1:
            raise Exception("Error getting the machine `%s` inside the lab." % machine_name)
        else:
            container = containers[0]

        if not shell:
            shell = Setting.get_instance().device_shell

        if logs and Setting.get_instance().print_startup_log:
            result = container.exec_run(cmd="cat /var/log/shared.log /var/log/startup.log",
                                        stdout=True,
                                        stderr=False,
                                        privileged=False,
                                        detach=False
                                        )

            result_string = result.output.decode('utf-8')
            if result_string:
                print("--- Startup Commands Log\n")
                print(result_string)
                print("--- End Startup Commands Log\n")

        def tty_connect():
            # Import PseudoTerminal only on Linux since some libraries are not available on Windows
            from ...trdparty.dockerpty.pty import PseudoTerminal

            # Needed with low level api because we need the id of the exec_create
            resp = self.client.api.exec_create(container.id,
                                               shell,
                                               stdout=True,
                                               stderr=True,
                                               stdin=True,
                                               tty=True,
                                               privileged=False
                                               )

            exec_output = self.client.api.exec_start(resp['Id'],
                                                     tty=True,
                                                     socket=True,
                                                     demux=True
                                                     )

            PseudoTerminal(self.client, exec_output, resp['Id']).start()

        def cmd_connect():
            Popen(["docker", "exec", "-it", container.id, shell])

        utils.exec_by_platform(tty_connect, cmd_connect, tty_connect)

    def get_machines_by_filters(self, lab_hash=None, machine_name=None, user=None):
        filters = {"label": ["app=kathara"]}
        if user:
            filters["label"].append("user=%s" % user)
        if lab_hash:
            filters["label"].append("lab_hash=%s" % lab_hash)
        if machine_name:
            filters["label"].append("name=%s" % machine_name)

        return self.client.containers.list(all=True, filters=filters)

    @staticmethod
    def get_container_name(name, lab_hash):
        lab_hash = lab_hash if lab_hash else ""
        return "%s_%s_%s_%s" % (Setting.get_instance().device_prefix, utils.get_current_user_name(), name, lab_hash)

    @staticmethod
    def delete_machine(machine):
        # Build the shutdown command string
        shutdown_commands_string = "; ".join(SHUTDOWN_COMMANDS).format(machine_name=machine.labels["name"])

        # Execute the shutdown commands inside the container (only if it's running)
        if machine.status == "running":
            machine.exec_run(cmd=[Setting.get_instance().device_shell, '-c', shutdown_commands_string],
                             stdout=False,
                             stderr=False,
                             privileged=True,
                             detach=True
                             )

        machine.remove(force=True)
