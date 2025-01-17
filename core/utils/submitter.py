"""
Module that has all classes used for request submission to computing
"""
import time
from core_lib.utils.ssh_executor import SSHExecutor
from core_lib.utils.locker import Locker
from core_lib.database.database import Database
from core_lib.utils.connection_wrapper import ConnectionWrapper
from core_lib.utils.submitter import Submitter as BaseSubmitter
from core_lib.utils.common_utils import clean_split, refresh_workflows_in_stats
from core_lib.utils.global_config import Config
from core.utils.emailer import Emailer


class RequestSubmitter(BaseSubmitter):
    """
    Subclass of base submitter that is tailored for RelVal submission
    """

    def add(self, relval, relval_controller):
        """
        Add a RelVal to the submission queue
        """
        prepid = relval.get_prepid()
        super().add_task(prepid,
                         self.submit_relval,
                         relval=relval,
                         controller=relval_controller)

    def __handle_error(self, relval, error_message):
        """
        Handle error that occured during submission, modify RelVal accordingly
        """
        self.logger.error(error_message)
        relval_db = Database('relvals')
        relval.set('status', 'new')
        relval.set('campaign_timestamp', 0)
        relval.add_history('submission', 'failed', 'automatic')
        for step in relval.get('steps'):
            step.set('config_id', '')
            step.set('resolved_globaltag', '')

        relval_db.save(relval.get_json())
        service_url = Config.get('service_url')
        emailer = Emailer()
        prepid = relval.get_prepid()
        subject = f'RelVal {prepid} submission failed'
        body = f'Hello,\n\nUnfortunately submission of {prepid} failed.\n'
        body += (f'You can find this relval at '
                 f'{service_url}/relvals?prepid={prepid}\n')
        body += f'Error message:\n\n{error_message}'
        recipients = emailer.get_recipients(relval)
        emailer.send(subject, body, recipients)

    def __handle_success(self, relval):
        """
        Handle notification of successful submission
        """
        prepid = relval.get_prepid()
        last_workflow = relval.get('workflows')[-1]['name']
        cmsweb_url = Config.get('cmsweb_url')
        self.logger.info('Submission of %s succeeded', prepid)
        service_url = Config.get('service_url')
        emailer = Emailer()
        subject = f'RelVal {prepid} submission succeeded'
        body = f'Hello,\n\nSubmission of {prepid} succeeded.\n'
        body += (f'You can find this relval at '
                 f'{service_url}/relvals?prepid={prepid}\n')
        body += f'Workflow in ReqMgr2 {cmsweb_url}/reqmgr2/fetch?rid={last_workflow}'
        if Config.get('development'):
            body += '\nNOTE: This was submitted from a development instance of RelVal machine '
            body += 'and this job will never start running in computing!\n'

        recipients = emailer.get_recipients(relval)
        emailer.send(subject, body, recipients)

    def prepare_workspace(self, relval, controller, ssh_executor, relval_dir):
        """
        Clean or create a remote directory and upload all needed files
        """
        prepid = relval.get_prepid()
        self.logger.info('Preparing workspace for %s', prepid)
        # Get cmsDriver script
        config_script = controller.get_cmsdriver(relval, for_submission=True)
        # Get config upload script
        upload_script = controller.get_config_upload_file(relval)

        # Re-create the directory and create a voms proxy there
        command = [f'rm -rf {relval_dir}',
                   f'mkdir -p {relval_dir}',
                   f'cd {relval_dir}',
                   'voms-proxy-init -voms cms --valid 4:00 --out $(pwd)/proxy.txt']
        ssh_executor.execute_command(command)

        # Upload config generation script - cmsDrivers
        ssh_executor.upload_as_file(config_script, f'{relval_dir}/config_generate.sh')
        # Upload config upload to ReqMgr2 script
        ssh_executor.upload_as_file(upload_script, f'{relval_dir}/config_upload.sh')
        # Upload python script used by upload script
        ssh_executor.upload_file('./core_lib/utils/config_uploader.py',
                                 f'{relval_dir}/config_uploader.py')

    def check_for_submission(self, relval):
        """
        Perform one last check of values before submitting a RelVal
        """
        self.logger.debug('Performing one last check for %s', relval.get_prepid())
        if relval.get('status') != 'submitting':
            raise Exception(f'Cannot submit a request with status {relval.get("status")}')

    def generate_configs(self, relval, ssh_executor, relval_dir):
        """
        SSH to a remote machine and generate cmsDriver config files
        """
        prepid = relval.get_prepid()
        command = [f'cd {relval_dir}',
                   'chmod +x config_generate.sh',
                   'export X509_USER_PROXY=$(pwd)/proxy.txt',
                   './config_generate.sh']
        stdout, stderr, exit_code = ssh_executor.execute_command(command)
        self.logger.debug('Exit code %s for %s config generation', exit_code, prepid)
        if exit_code != 0:
            raise Exception(f'Error generating configs for {prepid}.\n{stderr}')

        return stdout

    def upload_configs(self, relval, ssh_executor, relval_dir):
        """
        SSH to a remote machine and upload cmsDriver config files to ReqMgr2
        """
        prepid = relval.get_prepid()
        command = [f'cd {relval_dir}',
                   'chmod +x config_upload.sh',
                   'export X509_USER_PROXY=$(pwd)/proxy.txt',
                   './config_upload.sh']
        stdout, stderr, exit_code = ssh_executor.execute_command(command)
        self.logger.debug('Exit code %s for %s config upload', exit_code, prepid)
        if exit_code != 0:
            raise Exception(f'Error uploading configs for {prepid}.\n{stderr}')

        stdout = [x for x in clean_split(stdout, '\n') if 'DocID' in x]
        # Get all lines that have DocID as tuples split by space
        stdout = [tuple(clean_split(x.strip(), ' ')[1:]) for x in stdout]
        return stdout

    def update_steps_with_config_hashes(self, relval, config_hashes):
        """
        Iterate through RelVal steps and set config_id values
        """
        for step in relval.get('steps'):
            step_config_name = step.get_config_file_name()
            if not step_config_name:
                continue

            step_name = step.get('name')
            for config_pair in config_hashes:
                config_name, config_hash = config_pair
                if step_config_name == config_name:
                    step.set('config_id', config_hash)
                    config_hashes.remove(config_pair)
                    self.logger.debug('Set %s %s for %s',
                                      config_name,
                                      config_hash,
                                      step_name)
                    break
            else:
                raise Exception(f'Could not find hash for {step_name}')

        if config_hashes:
            raise Exception(f'Unused hashes: {config_hashes}')

        for step in relval.get('steps'):
            step_config_name = step.get_config_file_name()
            if not step_config_name:
                continue

            if not step.get('config_id'):
                step_name = step.get('name')
                raise Exception(f'Missing hash for step {step_name}')

    def submit_relval(self, relval, controller):
        """
        Method that is used by submission workers. This is where the actual submission happens
        """
        prepid = relval.get_prepid()
        credentials_file = Config.get('credentials_file')
        workspace_dir = Config.get('remote_path').rstrip('/')
        relval_dir = f'{workspace_dir}/{prepid}'
        prepid = relval.get_prepid()
        self.logger.debug('Will try to acquire lock for %s', prepid)
        with Locker().get_lock(prepid):
            self.logger.info('Locked %s for submission', prepid)
            relval_db = Database('relvals')
            relval = controller.get(prepid)
            try:
                self.check_for_submission(relval)
                with SSHExecutor('lxplus.cern.ch', credentials_file) as ssh:
                    # Start executing commands
                    self.prepare_workspace(relval, controller, ssh, relval_dir)
                    # Create configs
                    self.generate_configs(relval, ssh, relval_dir)
                    # Upload configs
                    config_hashes = self.upload_configs(relval, ssh, relval_dir)
                    # Remove remote relval directory
                    ssh.execute_command([f'rm -rf {relval_dir}'])

                self.logger.debug(config_hashes)
                # Iterate through uploaded configs and save their hashes in RelVal steps
                self.update_steps_with_config_hashes(relval, config_hashes)
                # Submit job dict to ReqMgr2
                job_dict = controller.get_job_dict(relval)
                cmsweb_url = Config.get('cmsweb_url')
                grid_cert = Config.get('grid_user_cert')
                grid_key = Config.get('grid_user_key')
                connection = ConnectionWrapper(host=cmsweb_url,
                                               cert_file=grid_cert,
                                               key_file=grid_key)
                workflow_name = self.submit_job_dict(job_dict, connection)
                # Update RelVal after successful submission
                relval.set('workflows', [{'name': workflow_name}])
                relval.set('status', 'submitted')
                relval.add_history('submission', 'succeeded', 'automatic')
                relval_db.save(relval.get_json())
                time.sleep(3)
                self.approve_workflow(workflow_name, connection)
                connection.close()
                if not Config.get('development'):
                    refresh_workflows_in_stats([workflow_name])

            except Exception as ex:
                self.__handle_error(relval, str(ex))
                return

            self.__handle_success(relval)

        if not Config.get('development'):
            controller.update_workflows(relval)

        self.logger.info('Successfully finished %s submission', prepid)
