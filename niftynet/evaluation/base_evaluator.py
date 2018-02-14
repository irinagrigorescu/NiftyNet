# -*- coding: utf-8 -*-
"""
This module defines base classes for Evaluator classes which define the
logic for iterating through the subjects and requested metrics needed for
evaluation
"""

from __future__ import absolute_import, division, print_function

import itertools
from itertools import groupby

from niftynet.evaluation.base_evaluations import ResultsDictionary
from niftynet.engine.application_factory import EvaluationFactory
from niftynet.utilities.util_common import cache

class BaseEvaluator(object):
    """
    The base evaluator defines a simple evaluations that iterates through
    subjects and computes each metric in sequence

    Sub-classes should overload the default_evaluation_list with
    application-specific metrics
    If a particular ordering of computations per subject is needed, sub-class
    can override the evaluate_next method; if a particular ordering of
    subjects is needed, subclasses can override the evaluate method.
    """
    def __init__(self, reader, app_param, eval_param):
        self.reader = reader
        self.app_param = app_param
        self.eval_param = eval_param
        if eval_param.evaluations:
            eval_list = eval_param.evaluations.split(',')
        else:
            eval_list = self.default_evaluation_list()
        evaluation_classes = [EvaluationFactory.create(e) for e in eval_list]
        self.evaluations = [e(reader, app_param, eval_param) for e in
                            evaluation_classes]

    def evaluate(self):
        """
        This method loops through all subjects and computes the metrics for
        each subject.

        :return: a ResultsDictionary object
        """
        def generator_from_reader(reader):
            while True:
                image_id, data, interp_orders = reader
                if image_id < 0:
                    break
                subject_id = self.reader.get_subject_id(image_id)
                yield (subject_id, data,interp_orders)
                
        generator = generator_from_reader(self.reader(shuffle=False))
        return self.evaluate_from_generator(generator)

    def evaluate_from_generator(self, generator):
        all_results = ResultsDictionary()
        for subject_id, data,interp_orders in generator:
            next_result = self.evaluate_next(subject_id, data,
                                             interp_orders)
            for group_by in next_result:
                all_results[group_by].extend(next_result[group_by])
        all_results = self.aggregate(all_results)
        return all_results

    def evaluate_next(self, subject_id, data, interp_orders):
        """
        Computes metrics for one subject.

        :param subject_id:
        :param data: data dictionary passed to each evaluation
        :param interp_orders: metadata for the data dictionary
               [currently not used]
        :return: a ResultsDictionary object
        """
        metrics = ResultsDictionary()

        for evaluation in self.evaluations:
            results = evaluation(subject_id, data)
            for group_by in results:
                metrics[group_by].extend(results[group_by])
        return metrics

    def aggregate(self, result_dict):
        """
        Apply all of the iterations requested by the evaluations

        :param result_dict: a dictionary as built by BaseEvaluator.evaluate()
        :return: a dictionary as built by BaseEvaluator.evaluate() but with
                 aggregations applied
        """
        aggregations = []
        for evaluation in self.evaluations:
            agg_list = evaluation.get_aggregations()
            print(evaluation, agg_list)
            aggregations.extend(agg_list)
        for aggregation in aggregations:
            result_dict = aggregation(result_dict)
        return result_dict

    def default_evaluation_list(self):
        """
        :return: List of EvaluationFactory strings defining the evaluations
        to compute if no evaluations are specified in the configuration
        """
        raise NotImplementedError('not implemented in abstract class')

class CachedSubanalysisEvaluator(BaseEvaluator):
    """
    This evaluator sequences evaluations in a way that is friendly for
    caching intermediate computations. Each evaluation defines sub-analyses
    to run, and all subanalysis are run at the same time then the cache is
    cleared
    """

    def evaluate_next(self, subject_id, data, interp_orders):
        """
        Computes metrics for one subject. Instead of iterating through the
        metrics in order, this method first identifies sub-analyses that should
        be run together (for caching reasons) and iterates through the
        sub-analyses in sequence, calculating the metrics for each
        sub-analysis together

        :param subject_id:
        :param data: data dictionary passed to each evaluation
        :param interp_orders: metadata for the data dictionary
               [currently not used]
        :return: a ResultsDictionary object
        """
        # First go through evaluations to find those with subanalyses
        evaluations = {'normal': [], 'subanalyses':[]}
        for evl in self.evaluations:
            if hasattr(evl, 'subanalyses'):
                sub = evl.subanalyses(subject_id, data)
                evaluations['subanalyses'].extend([(evl, s) for s in sub])
            else:
                evaluations['normal'].append(evl)

        # Run normal evaluations
        metrics = ResultsDictionary()
        for evaluation in evaluations['normal']:
            results = evaluation(subject_id, data)
            for group_by in results:
                metrics[group_by].extend(results[group_by])

        # group sub-analysis evaluations by subanalysis
        def keyfunc(sub):
            return str(sub[1])
        tasks = sorted(evaluations['subanalyses'], key=keyfunc)
        tasksets = groupby(tasks, keyfunc)
        # run grouped evaluations
        for _, evaluationset in tasksets:
            for evaluation, sub in evaluationset:
                results = evaluation(subject_id, data, sub)
                for group_by in results:
                    metrics[group_by].extend(results[group_by])
            cache.clear()
        return metrics


class ScalarAggregator(object):
    """
    This class defines a simple aggregator that groups metrics and applies an
    aggregating function. Grouping is determined by the set difference
    between an original `group_by` term and a subset `new_group_py` term.
    """
    def __init__(self, key, group_by, new_group_by, func, name):
        """

        :param key: metric heading name with values to aggregate
        :param group_by: level at which original metric was computed,
            e.g. ('subject_id', 'label')
        :param new_group_by: level at which metric after aggregation is
            computed, e.g. ('label')
        :param func: function (iterable=>scalar) to aggregate the collected
        values e.g., np.mean
        :param name: new heading name for the aggregated metric
        """
        self.key = key
        self.name = name
        self.group_by = group_by
        self.new_group_by = new_group_by
        self.func = func

    def __call__(self, result_dict):
        """
        Perform the aggregation by manipulating result_dict
        :param result_dict: a dictionary as built by BaseEvaluator.evaluate()
        :return: a dictionary as built by BaseEvaluator.evaluate()
        """
        entries = result_dict.get(self.group_by, [])
        matched_entries = filter(lambda x: self.key in x, entries)
        def keyfunc(entry):
            return tuple((k, entry[k]) for k in self.new_group_by)
        matched_entries = sorted(matched_entries, key=keyfunc)
        for key, group in itertools.groupby(matched_entries, keyfunc):
            values = [i[self.key] for i in group]
            aggregated_value = self.func(values)
            base_dict = {k: v for k, v in key}
            base_dict[self.name] = aggregated_value
            result_dict[self.new_group_by].append(base_dict)
        return result_dict
