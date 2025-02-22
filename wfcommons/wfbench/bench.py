#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (c) 2021-2022 The WfCommons Team.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

import glob
import json
import logging
import os
import pathlib
import subprocess
import time
import uuid
import sys

from logging import Logger
from typing import Dict, Optional, List, Set, Type, Union

from numpy import isin

from ..common import File, FileLink, Task

from ..wfchef.wfchef_abstract_recipe import WfChefWorkflowRecipe
from ..wfgen import WorkflowGenerator

this_dir = pathlib.Path(__file__).resolve().parent

logging.basicConfig(stream=sys.stdout, level=logging.INFO)


class WorkflowBenchmark:
    """Generate a workflow benchmark instance based on a workflow recipe (WfChefWorkflowRecipe)

    :param recipe: A workflow recipe.
    :type recipe: Type[WfChefWorkflowRecipe]
    :param num_tasks: Total number of tasks in the benchmark workflow.
    :type num_tasks: int
    :param logger: The logger where to log information/warning or errors.
    :type logger: Optional[Logger]
    """

    def __init__(self,
                 recipe: Type[WfChefWorkflowRecipe],
                 num_tasks: int,
                 logger: Optional[Logger] = None) -> None:
        """Create an object that represents a workflow benchmark generator."""
        self.logger: Logger = logging.getLogger(
            __name__) if logger is None else logger
        self.recipe = recipe
        self.num_tasks = num_tasks
        self.workflow = None

    def create_benchmark_from_input_file(self,
                                         save_dir: pathlib.Path,
                                         input_file: pathlib.Path,
                                         lock_files_folder: Optional[pathlib.Path] = None) -> pathlib.Path:
        """Create a workflow benchmark.

        :param save_dir: Folder to generate the workflow benchmark JSON instance and input data files.
        :type save_dir: pathlib.Path
        :param input_file: 
        :type input_file: pathlib.Path
        :param lock_files_folder:
        :type lock_files_folder: Optional[pathlib.Path]

        :return: The path to the workflow benchmark JSON instance.
        :rtype: pathlib.Path
        """
        params = json.loads(input_file.read_text())
        return self.create_benchmark(save_dir, lock_files_folder=lock_files_folder, **params)

    def create_benchmark(self,
                         save_dir: pathlib.Path,
                         percent_cpu: Union[float, Dict[str, float]] = 0.6,
                         cpu_work: Union[int, Dict[str, int]] = None,
                         gpu_work: Union[int, Dict[str, int]] = None,
                         data: Optional[Union[int, Dict[str, str]]] = None,
                         lock_files_folder: Optional[pathlib.Path] = None,
                         regenerate: Optional[bool] = True) -> pathlib.Path:
        """Create a workflow benchmark.

        :param save_dir: Folder to generate the workflow benchmark JSON instance and input data files.
        :type save_dir: pathlib.Path
        :param percent_cpu: The percentage of CPU threads.
        :type percent_cpu: Union[float, Dict[str, float]]
        :param cpu_work: CPU work per workflow task.
        :type cpu_work: Union[int, Dict[str, int]]
        :param data: Dictionary of input size files per workflow task type or total workflow data footprint (in MB).
        :type data: Optional[Union[int, Dict[str, str]]]
        :param lock_files_folder:
        :type lock_files_folder: Optional[pathlib.Path]
        :param regenerate: Whether to regenerate the workflow tasks
        :type regenerate: Optional[bool]

        :return: The path to the workflow benchmark JSON instance.
        :rtype: pathlib.Path
        """
        save_dir = save_dir.resolve()
        save_dir.mkdir(exist_ok=True, parents=True)

        if not self.workflow or regenerate:
            self.logger.debug("Generating workflow")
            generator = WorkflowGenerator(
                self.recipe.from_num_tasks(self.num_tasks))
            self.workflow = generator.build_workflow()
            self.workflow.name = f"{self.workflow.name.split('-')[0]}-Benchmark"
        json_path = save_dir.joinpath(
            f"{self.workflow.name.lower()}-{self.num_tasks}").with_suffix(".json")

        # Creating the lock files
        if lock_files_folder:
            try:
                lock_files_folder.mkdir(exist_ok=True, parents=True)
                self.logger.debug(
                    f"Creating lock files at: {lock_files_folder.resolve()}")
                lock = lock_files_folder.joinpath("cores.txt.lock")
                cores = lock_files_folder.joinpath("cores.txt")
                with lock.open("w+"), cores.open("w+"):
                    pass
            except (FileNotFoundError, OSError) as e:
                self.logger.warning(f"Could not find folder to create lock files: {lock_files_folder.resolve()}\n"
                                    f"You will need to create them manually: 'cores.txt.lock' and 'cores.txt'")

        # Setting the parameters for the arguments section of the JSON
        for task in self.workflow.tasks.values():
            params = []

            if cpu_work:
                _percent_cpu = percent_cpu[task.category] if isinstance(
                    percent_cpu, dict) else percent_cpu
                _cpu_work = cpu_work[task.category] if isinstance(
                    cpu_work, dict) else cpu_work

                params.extend([f"--percent-cpu {_percent_cpu}",
                               f"--cpu-work {_cpu_work}"])

                if lock_files_folder:
                    params.extend([f"--path-lock {lock}",
                                   f"--path-cores {cores}"])

            # Setting gpu arguments if gpu benchmark requested
            if gpu_work:
                _gpu_work = gpu_work[task.category] if isinstance(
                    gpu_work, dict) else gpu_work

                params.extend([f"--gpu-work {_gpu_work}"])

            task.runtime = 0
            task.files = []
            task.program = f"{this_dir.joinpath('wfbench.py')}"
            task.args = [task.name]
            task.args.extend(params)

        # task's data footprint provided as individual data input size (JSON file)
        if isinstance(data, dict):
            outputs = self._output_files(data)
            for task in self.workflow.tasks.values():
                outputs_file_size = {}
                for child, data_size in outputs[task.name].items():
                    outputs_file_size[f"{task.name}_{child}_output.txt"] = data_size

                task.args.extend([f"--out {outputs_file_size}"])

            self._add_output_files(outputs)
            self._add_input_files(outputs, data)
            self.logger.debug("Generating system files.")
            self._generate_data_for_root_nodes(save_dir, data)

        # data footprint provided as an integer
        elif isinstance(data, int):
            num_sys_files, num_total_files = self._calculate_input_files()
            self.logger.debug(
                f"Number of input files to be created by the system: {num_sys_files}")
            self.logger.debug(
                f"Total number of files used by the workflow: {num_total_files}")
            file_size = round(data * 1000000 / num_total_files)  # MB to B
            self.logger.debug(
                f"Every input/output file is of size: {file_size}")

            for task in self.workflow.tasks.values():
                output = {f"{task.name}_output.txt": file_size}
                task.args.extend([f"--out {output}"])
                outputs = {}
                if self.workflow.tasks_children[task.name]:
                    outputs.setdefault(task.name, {})
                    for child in self.workflow.tasks_children[task.name]:
                        outputs[task.name][child] = file_size

            self._add_output_files(file_size)
            self._add_input_files(outputs, file_size)
            self.logger.debug("Generating system files.")
            self._generate_data_for_root_nodes(save_dir, file_size)

        self.logger.info(f"Saving benchmark workflow: {json_path}")
        self.workflow.write_json(json_path)
        # json_path.write_text(json.dumps(wf, indent=4))
        # self.workflow.workflow_json = wf

        return json_path

    def _output_files(self, data: Dict[str, str]) -> Dict[str, Dict[str, int]]:
        """
        Calculate, for each task, total number of output files needed.
        This method is used when the user is specifying the input file sizes.

        :param data:
        :type data: Dict[str, str]

        :return: 
        :rtype: Dict[str, Dict[str, int]]
        """
        output_files = {}
        for task in self.workflow.tasks.values():
            output_files.setdefault(task.name, {})
            if not self.workflow.tasks_children[task.name]:
                output_files[task.name][task.name] = int(data[task.category])
            else:
                for child_name in self.workflow.tasks_children[task.name]:
                    child = self.workflow.tasks[child_name]
                    output_files[task.name][child.name] = int(
                        data[child.category])

        return output_files

    def _calculate_input_files(self):
        """
        Calculate total number of files needed.
        This mehtod is used if the user provides total datafootprint.
        """
        tasks_need_input = 0
        tasks_dont_need_input = 0

        for task in self.workflow.tasks.values():
            parents = self.workflow.tasks_parents[task.name]
            if not parents:
                tasks_need_input += 1
            else:
                tasks_dont_need_input += 1

        total_num_files = tasks_need_input * 2 + tasks_dont_need_input

        return tasks_need_input, total_num_files

    def _add_output_files(self, output_files: Union[int, Dict[str, Dict[str, int]]]) -> None:
        """
        Add output files when input data was offered by the user.

        :param output_files:
        :type wf: Union[int, Dict[str, Dict[str, int]]]
        """
        for task in self.workflow.tasks.values():
            if isinstance(output_files, Dict):
                for child, file_size in output_files[task.name].items():
                    task.files.append(
                        File(f"{task.name}_{child}_output.txt", file_size, FileLink.OUTPUT))
            elif isinstance(output_files, int):
                task.files.append(
                    File(f"{task.name}_output.txt", output_files, FileLink.OUTPUT))

    def _add_input_files(self, output_files: Dict[str, Dict[str, str]], data: Union[int, Dict[str, str]]) -> None:
        """
        Add input files when input data was offered by the user.

        :param output_files:
        :type wf: Dict[str, Dict[str, str]]
        :param data:
        :type data: Union[int, Dict[str, str]]
        """
        input_files = {}
        for parent, children in output_files.items():
            for child, file_size in children.items():
                input_files.setdefault(child, {})
                input_files[child][parent] = file_size

        for task in self.workflow.tasks.values():
            inputs = []
            if not self.workflow.tasks_parents[task.name]:
                task.files.append(
                    File(f"{task.name}_input.txt",
                         data[task.category] if isinstance(
                             data, Dict) else data,
                         FileLink.INPUT))
                inputs.append(f'{task.name}_input.txt')
            else:
                if isinstance(data, Dict):
                    for parent, file_size in input_files[task.name].items():
                        task.files.append(
                            File(f"{parent}_{task.name}_output.txt", file_size, FileLink.INPUT))
                        inputs.append(f"{parent}_{task.name}_output.txt")

                elif isinstance(data, int):
                    for parent in self.workflow.tasks_parents[task.name]:
                        task.files.append(
                            File(f"{parent}_output.txt", data, FileLink.INPUT))
                        inputs.append(f"{parent}_output.txt")

            task.args.extend(inputs)

    def _generate_data_for_root_nodes(self, save_dir: pathlib.Path, data: Union[int, Dict[str, str]]) -> None:
        """
        Generate workflow's input data for root nodes based on user's input.
        
        :param save_dir:
        :type save_dir: pathlib.Path
        :param data:
        :type data: Dict[str, str]
        """
        for task in self.workflow.tasks.values():
            if not self.workflow.tasks_parents[task.name]:
                file_size = data[task.category] if isinstance(
                    data, Dict) else data
                file = save_dir.joinpath(f"{task.name}_input.txt")
                if not file.is_file():
                    with open(file, 'wb') as fp:
                        fp.write(os.urandom(int(file_size)))
                    self.logger.debug(f"Created file: {str(file)}")

    def generate_input_file(self, path: pathlib.Path) -> None:
        """
        Generates input file where customization of cpu percentage, cpu work, gpu work, data size
        
        :param path:
        :type path: pathlib.Path
        """
        generator = WorkflowGenerator(
            self.recipe.from_num_tasks(self.num_tasks))
        workflow = generator.build_workflow()

        defaults = {
            "percent_cpu": 0.6,
            "cpu_work": 1000,
            "gpu_work": 100,
            "data": 10
        }
        inputs = {
            "percent_cpu": {},
            "cpu_work": {},
            "gpu_work": {},
            "data": {}

        }
        for node in workflow.nodes:
            task: Task = workflow.nodes[node]['task']
            task_type = task.name.split("_0")[0]

            for key in inputs.keys():
                inputs[key].setdefault(task_type, defaults[key])

        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text(json.dumps(inputs, indent=2))
        input("Please fill up the input file and press ENTER to continue...")

    def run(self, json_path: pathlib.Path, save_dir: pathlib.Path) -> None:
        """
        Run the benchmark workflow locally (for test purposes only).

        :param json_path:
        :type json_path: pathlib.Path
        :param: save_dir:
        :type save_dir: pathlib.Path
        """
        self.logger.debug("Running")
        try:
            wf = json.loads(json_path.read_text())
            with save_dir.joinpath(f"run.txt").open("w+") as fp:
                has_executed: Set[str] = set()
                procs: List[subprocess.Popen] = []
                while len(has_executed) < len(wf["workflow"]["tasks"]):
                    for task in wf["workflow"]["tasks"]:
                        if task["name"] in has_executed:
                            continue
                        ready_to_execute = all([
                            this_dir.joinpath(input_file["name"]).exists()
                            for input_file in task["files"]
                            if input_file["link"] == "input"
                        ])
                        if not ready_to_execute:
                            continue
                        has_executed.add(task["name"])

                        executable = task["command"]["program"]
                        arguments = task["command"]["arguments"]
                        if "--out" in arguments:
                            files = assigning_correct_files(task)
                            program = ["time", "python",
                                       executable, *arguments, *files]
                        else:
                            program = ["time", "python",
                                       executable, *arguments]
                        folder = pathlib.Path(this_dir.joinpath(
                            f"wfbench_execution/{uuid.uuid4()}"))
                        folder.mkdir(exist_ok=True, parents=True)
                        os.chdir(str(folder))
                        procs.append(subprocess.Popen(
                            program, stdout=fp, stderr=fp))
                        os.chdir("../..")

                    time.sleep(1)
                for proc in procs:
                    proc.wait()
            cleanup_sys_files()

        except Exception as e:
            subprocess.Popen(["killall", "stress-ng"])
            cleanup_sys_files()
            import traceback
            traceback.print_exc()
            raise FileNotFoundError("Not able to find the executable.")


def generate_sys_data(num_files: int, file_total_size: int, task_name: List[str], save_dir: pathlib.Path) -> None:
    """Generate workflow's input data

    :param num_files:
    :type num_files: int
    :param file_total_size:
    :type file_total_size: int
    :param save_dir: Folder to generate the workflow benchmark's input data files.
    :type save_dir: pathlib.Path
    """
    for _ in range(num_files):
        for name in task_name:
            file = f"{save_dir.joinpath(f'{name}_input.txt')}"
            with open(file, 'wb') as fp:
                fp.write(os.urandom(file_total_size))
            print(f"Created file: {file}")


def assigning_correct_files(task: Dict[str, str]) -> List[str]:
    files = []
    for file in task["files"]:
        if file["link"] == "input":
            files.append(file["name"])
    return files


def cleanup_sys_files() -> None:
    """Remove files already used"""
    input_files = glob.glob("*input*.txt")
    output_files = glob.glob("*output.txt")
    all_files = input_files + output_files
    for t in all_files:
        os.remove(t)
