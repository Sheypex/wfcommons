#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (c) 2020 The WorkflowHub Team.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

import logging
import uuid

from abc import ABC, abstractmethod
from os import path
from logging import Logger
from typing import Any, Dict, List, Optional

from ...common.file import File, FileLink
from ...common.job import Job, JobType
from ...common.workflow import Workflow
from ...utils import generate_rvs


class WorkflowRecipe(ABC):
    """An abstract class of workflow recipes for creating synthetic workflow traces.

    :param name: The workflow recipe name.
    :type name: str
    :param data_footprint: The upper bound for the workflow total data footprint (in bytes).
    :type data_footprint: int
    :param num_jobs: The upper bound for the total number of jobs in the workflow.
    :type num_jobs: int
    :param logger: The logger where to log information/warning or errors (optional).
    :type logger: Logger
    """

    def __init__(self, name: str, data_footprint: Optional[int], num_jobs: Optional[int],
                 logger: Optional[Logger] = None) -> None:
        """Create an object of the workflow recipe."""
        self.logger = logging.getLogger(__name__) if logger is None else logger
        self.name = name
        self.data_footprint = data_footprint
        self.num_jobs = num_jobs
        self.workflows: List[Workflow] = []
        self.jobs_files: Dict[str, List[File]] = {}
        self.job_id_counter = 1

    @abstractmethod
    def _workflow_recipe(self) -> Dict[str, Any]:
        """Recipe for generating synthetic traces for a workflow. Recipes can be
        generated by using the :class:`~workflowhub.trace.trace_analyzer.TraceAnalyzer`.

        :return: A recipe in the form of a dictionary in which keys are job prefixes.
        :rtype: Dict[str, Any]
        """

    @classmethod
    @abstractmethod
    def from_num_jobs(cls, num_jobs: int) -> 'WorkflowRecipe':
        """
        Instantiate a workflow recipe that will generate synthetic workflows up to the
        total number of jobs provided.

        :param num_jobs: The upper bound for the total number of jobs in the workflow.
        :type num_jobs: int

        :return: A workflow recipe object that will generate synthetic workflows up to
                 the total number of jobs provided.
        :rtype: WorkflowRecipe
        """

    @abstractmethod
    def build_workflow(self, workflow_name: Optional[str] = None) -> Workflow:
        """Generate a synthetic workflow trace.

        :param workflow_name: The workflow name.
        :type workflow_name: str

        :return: A synthetic workflow trace object.
        :rtype: Workflow
        """

    def _generate_job(self, job_name: str, job_id: str, input_files: Optional[List[File]] = None,
                      files_recipe: Optional[Dict[FileLink, Dict[str, int]]] = None) -> Job:
        """Generate a synthetic job.

        :param job_name: Job name.
        :type job_name: str
        :param job_id: Job ID.
        :type job_id: str
        :param input_files: List of input files to be included.
        :type input_files: List[File]
        :param files_recipe: Recipe for generating job files.
        :type files_recipe: Dict[FileLink, Dict[str, int]]

        :return: A job object.
        :rtype: Job
        """
        job_recipe = self._workflow_recipe()[job_name]

        # runtime
        runtime: float = float(format(generate_rvs(job_recipe['runtime']['distribution'],
                                                   job_recipe['runtime']['min'],
                                                   job_recipe['runtime']['max']), '.3f'))

        # linking previous generated output files as input files
        self.jobs_files[job_id] = []
        if input_files:
            for f in input_files:
                if f.link == FileLink.OUTPUT:
                    self.jobs_files[job_id].append(File(name=f.name, size=f.size, link=FileLink.INPUT))

        # generate additional in/output files
        self._generate_files(job_id, job_recipe['input'], FileLink.INPUT, files_recipe)
        self._generate_files(job_id, job_recipe['output'], FileLink.OUTPUT, files_recipe)

        return Job(
            name=job_id,
            job_type=JobType.COMPUTE,
            runtime=runtime,
            machine=None,
            args=[],
            cores=1,
            avg_cpu=None,
            bytes_read=None,
            bytes_written=None,
            memory=None,
            energy=None,
            avg_power=None,
            priority=None,
            files=self.jobs_files[job_id]
        )

    def _generate_job_name(self, prefix: str) -> str:
        """Generate a job name from a prefix appended with an ID.

        :param prefix: Job prefix.
        :type prefix: str

        :return: Job name from prefix appended with an ID.
        :rtype: str
        """
        job_name = "{}_{:08d}".format(prefix, self.job_id_counter)
        self.job_id_counter += 1
        return job_name

    def _generate_files(self, job_id: str, recipe: Dict[str, Any], link: FileLink,
                        files_recipe: Optional[Dict[FileLink, Dict[str, int]]] = None) -> None:
        """Generate files for a specific job ID.

        :param job_id: Job ID.
        :type job_id: str
        :param recipe: Recipe for generating the job.
        :type recipe: Dict[str, Any]
        :param link: Type of file link.
        :type link: FileLink
        :param files_recipe: Recipe for generating job files.
        :type files_recipe: Dict[FileLink, Dict[str, int]]
        """
        extension_list: List[str] = []
        for f in self.jobs_files[job_id]:
            if f.link == link:
                extension_list.append(path.splitext(f.name)[1] if '.' in f.name else f.name)

        for extension in recipe:
            if extension not in extension_list:
                num_files = 1
                if files_recipe and link in files_recipe and extension in files_recipe[link]:
                    num_files = files_recipe[link][extension]
                for _ in range(0, num_files):
                    self.jobs_files[job_id].append(self._generate_file(extension, recipe, link))

    def _generate_file(self, extension: str, recipe: Dict[str, Any], link: FileLink) -> File:
        """Generate a file according to a file recipe.

        :param extension:
        :type extension: str
        :param recipe: Recipe for generating the file.
        :type recipe: Dict[str, Any]
        :param link: Type of file link.
        :type link: FileLink

        :return: The generated file.
        :rtype: File
        """
        return File(name=str(uuid.uuid4()) + extension,
                    link=link,
                    size=int(generate_rvs(recipe[extension]['distribution'],
                                          recipe[extension]['min'],
                                          recipe[extension]['max'])))

    def _get_files_by_job_and_link(self, job_id: str, link: FileLink) -> List[File]:
        """Get the list of files for a job ID and link type.

        :param job_id: Job ID.
        :type job_id: str
        :param link: Type of file link.
        :type link: FileLink

        :return: List of files for a job ID and link type.
        :rtype: List[File]
        """
        files_list: List[File] = []
        for f in self.jobs_files[job_id]:
            if f.link == link:
                files_list.append(f)
        return files_list
