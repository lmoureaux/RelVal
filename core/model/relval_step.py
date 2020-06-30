"""
Module that contains RelValStep class
"""
import weakref
from copy import deepcopy
from core.model.model_base import ModelBase


class RelValStep(ModelBase):

    _ModelBase__schema = {
        # PrepID
        'name': '',
        # Arguments if step is a cmsDriver step
        'arguments': {},
        # Input information if step is list of input files
        'input': {},
    }

    lambda_checks = {

    }

    def __init__(self, json_input=None, parent=None):
        self.parent = None
        if json_input:
            json_input = deepcopy(json_input)
            # Remove -- from argument names
            json_input['arguments'] = {k.lstrip('-'): v for k, v in json_input['arguments'].items()}

        ModelBase.__init__(self, json_input)
        if parent:
            self.parent = weakref.ref(parent)

    def get_index_in_parent(self):
        """
        Return step's index in parent's list of steps
        """
        for index, step in enumerate(self.parent().get('steps')):
            if self == step:
                return index

        raise Exception(f'Sequence is not a child of {self.parent().get_prepid()}')

    def get_step_type(self):
        """
        Return whether this is cmsDriver or input file step
        """
        if self.get('input'):
            return 'input_file'

        return 'cmsDriver'

    def __build_cmsdriver(self, cmsdriver_type, arguments):
        """
        Build a cmsDriver command from given arguments
        Add comment in front of the command
        """
        self.logger.info('Generating %s cmdDriver', cmsdriver_type)
        # Actual command
        command = f'# Command for {cmsdriver_type}:\ncmsDriver.py {cmsdriver_type}'
        # Comment in front of the command for better readability
        comment = f'# Arguments for {cmsdriver_type}:\n'
        for key in sorted(arguments.keys()):
            if not arguments[key]:
                continue

            if key in 'extra':
                continue

            if isinstance(arguments[key], bool):
                arguments[key] = ''

            if isinstance(arguments[key], list):
                arguments[key] = ','.join([str(x) for x in arguments[key]])

            command += f' --{key} {arguments[key]}'.rstrip()
            comment += f'# --{key} {arguments[key]}'.rstrip() + '\n'

        if arguments.get('extra'):
            extra_value = arguments['extra']
            command += f' {extra_value}'
            comment += f'# <extra> {extra_value}\n'

        # Exit the script with error of cmsDriver.py
        command += ' || exit $?'

        return comment + '\n' + command

    def get_cmsdriver(self):
        """
        Return a cmsDriver command for this step
        Config file is named like this
        """
        arguments_dict = dict(self.get_json()).get('arguments', {})
        # Delete sequence metadata
        if 'config_id' in arguments_dict:
            del arguments_dict['config_id']

        if 'harvesting_config_id' in arguments_dict:
            del arguments_dict['harvesting_config_id']

        # Handle input/output file names
        index = self.get_index_in_parent()
        name = self.get('name')
        step_type = self.get_step_type()
        if index == 0:
            if step_type == 'input_file':
                pass
            else:
                arguments_dict['fileout'] = f'"file:step{index + 1}.root"'
                arguments_dict['python_filename'] = f'{name}.py'

        else:
            previous_step = self.parent().get('steps')[index - 1]
            previous_step_type = previous_step.get_step_type()
            if previous_step_type == 'input_file':
                arguments_dict['filein'] = f'"filelist:step{index}_files.txt"'
                arguments_dict['lumiToProcess'] = f'"step{index}_lumi_ranges.txt"'
            else:
                if 'HARVESTING' in arguments_dict['step']:
                    arguments_dict['filein'] = f'"file:step{index}_inDQM.root"'
                else:
                    arguments_dict['filein'] = f'"file:step{index}.root"'

            arguments_dict['fileout'] = f'"file:step{index + 1}.root"'
            arguments_dict['python_filename'] = f'{name}.py'

        arguments_dict['no_exec'] = True
        cms_driver_command = self.__build_cmsdriver(f'step{index + 1}', arguments_dict)
        return cms_driver_command