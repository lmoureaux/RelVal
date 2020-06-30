"""
Module that contains TicketController class
"""
import json
import os
from random import Random
from core.model.ticket import Ticket
from core.model.relval import RelVal
from core.model.campaign import Campaign
from core.controller.controller_base import ControllerBase
from core.controller.relval_controller import RelValController
from core.database.database import Database
from core.utils.ssh_executor import SSHExecutor
from core.utils.settings import Settings


class TicketController(ControllerBase):

    def __init__(self):
        ControllerBase.__init__(self)
        self.database_name = 'tickets'
        self.model_class = Ticket

    def create(self, json_data):
        # Clean up the input
        campaign_name = json_data.get('campaign')
        campaign_db = Database('campaigns')
        campaign_json = campaign_db.get(campaign_name)
        if not campaign_json:
            raise Exception(f'Campaign {campaign_name} does not exist')

        campaign = Campaign(json_input=campaign_json)
        cmssw_release = campaign.get('cmssw_release')
        batch_name = campaign.get('batch_name')
        prepid_part = f'{cmssw_release}__{batch_name}'
        json_data['prepid'] = f'{prepid_part}-00000'
        settings = Settings()
        with self.locker.get_lock(f'generate-ticket-prepid'):
            # Get a new serial number
            serial_numbers = settings.get('tickets_prepid_sequence', {})
            serial_number = serial_numbers.get(prepid_part, 0)
            serial_number += 1
            # Form a new temporary prepid
            prepid = f'{prepid_part}-{serial_number:05d}'
            json_data['prepid'] = prepid
            relval = super().create(json_data)
            # After successful save update serial numbers in settings
            serial_numbers[prepid_part] = serial_number
            settings.save('tickets_prepid_sequence', serial_numbers)
            return relval

    def get_editing_info(self, obj):
        editing_info = super().get_editing_info(obj)
        prepid = obj.get_prepid()
        status = obj.get('status')
        creating_new = not bool(prepid)
        not_done = status != 'done'
        editing_info['prepid'] = False
        editing_info['campaign'] = creating_new
        editing_info['cpu_cores'] = not_done
        editing_info['extension_number'] = not_done
        editing_info['events'] = not_done
        editing_info['label'] = not_done
        editing_info['memory'] = not_done
        editing_info['notes'] = True
        editing_info['processing_string'] = not_done
        editing_info['sample_tag'] = not_done
        editing_info['workflow_ids'] = not_done
        editing_info['relval_set'] = not_done
        
        return editing_info

    def check_for_delete(self, obj):
        created_relvals = obj.get('created_relvals')
        prepid = obj.get('prepid')
        if created_relvals:
            raise Exception(f'It is not allowed to delete tickets that have relvals created. '
                            f'{prepid} has {len(created_relvals)} relvals')

        return True

    def create_relvals_for_ticket(self, ticket):
        """
        Create RelVals from given subcampaign ticket. Return list of relval prepids
        """
        ticket_db = Database(self.database_name)
        campaign_db = Database('campaigns')
        ticket_prepid = ticket.get_prepid()
        credentials_path = Settings().get('credentials_path')
        ssh_executor = SSHExecutor('lxplus.cern.ch', credentials_path)
        relval_controller = RelValController()
        created_relvals = []
        with self.locker.get_lock(ticket_prepid):
            ticket = Ticket(json_input=ticket_db.get(ticket_prepid))
            campaign_name = ticket.get('campaign')
            campaign = Campaign(json_input=campaign_db.get(campaign_name))
            relval_set = ticket.get('relval_set')
            cmssw_release = campaign.get('cmssw_release')
            processing_string = ticket.get('processing_string')
            extension_number = ticket.get('extension_number')
            sample_tag = ticket.get('sample_tag')
            events = ticket.get('events')
            try:
                workflow_ids = ','.join([str(x) for x in ticket.get('workflow_ids')])
                self.logger.info('Creating RelVals %s for %s', workflow_ids, ticket_prepid)
                # Prepare empty directory with runTheMatrixPdmV.py
                command = [f'rm -rf ~/relval_work/{ticket_prepid}',
                           f'mkdir -p ~/relval_work/{ticket_prepid}']
                out, err, code = ssh_executor.execute_command(command)
                if code != 0:
                    self.logger.error('Exit code %s preparing workspace:\nError:%s\nOutput:%s',
                                      code,
                                      err,
                                      out)
                    raise Exception(f'Error code {code} preparing workspace: {err}')

                ssh_executor.upload_file('core/utils/runTheMatrixPdmV.py',
                                         f'relval_work/{ticket_prepid}/runTheMatrixPdmV.py')
                # Create a random name for temporary file
                random = Random()
                file_name = f'{ticket_prepid}_{int(random.randint(1000, 9999))}.json'
                self.logger.info('Random file name %s', file_name)
                # Execute runTheMatrixPdmV.py
                command = ['cd ~/relval_work/',
                           'source /cvmfs/cms.cern.ch/cmsset_default.sh',
                           f'if [ -r {cmssw_release}/src ] ; then echo {cmssw_release} already exist',
                           f'else scram p CMSSW {cmssw_release}',
                           'fi',
                           f'cd {cmssw_release}/src',
                           'eval `scram runtime -sh`',
                           f'cd ../../{ticket_prepid}',
                           'python runTheMatrixPdmV.py -l %s -w %s -o %s' % (workflow_ids,
                                                                             relval_set,
                                                                             file_name)]
                out, err, code = ssh_executor.execute_command(command)
                if code != 0:
                    self.logger.error('Exit code %s creating RelVals:\nError:%s\nOutput:%s',
                                      code,
                                      err,
                                      out)
                    raise Exception(f'Error code {code} creating RelVals: {err}')

                ssh_executor.download_file(f'relval_work/{ticket_prepid}/{file_name}',
                                           f'/tmp/{file_name}')
                with open(f'/tmp/{file_name}', 'r') as workflows_file:
                    workflows = json.load(workflows_file)

                for workflow_id, workflow_dict in workflows.items():
                    workflow_json = {'workflow_id': workflow_id,
                                     'relval_set': relval_set,
                                     'processing_string': processing_string,
                                     'campaign': campaign_name,
                                     'extension_number': extension_number,
                                     'sample_tag': sample_tag,
                                     'events': events,
                                     'steps': []}
                    for step_dict in workflow_dict['steps']:
                        workflow_json['steps'].append({'name': step_dict['name'],
                                                       'arguments': step_dict.get('arguments', {}),
                                                       'input': step_dict.get('input', {})})

                    relval = relval_controller.create(workflow_json)
                    created_relvals.append(relval)
                    self.logger.info('Created %s', relval.get_prepid())

                os.remove(f'/tmp/{file_name}')
                created_relval_prepids = [r.get('prepid') for r in created_relvals]
                ticket.set('created_relvals', created_relval_prepids)
                ticket.set('status', 'done')
                ticket.add_history('created_relvals', created_relval_prepids, None)
                ticket_db.save(ticket.get_json())
            except Exception as ex:
                # Delete created relvals if there was an Exception
                for created_relval in reversed(created_relvals):
                    relval_controller.delete({'prepid': created_relval.get('prepid')})

                # And reraise the exception
                raise ex

        return []