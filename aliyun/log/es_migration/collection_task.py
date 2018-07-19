#!/usr/bin/env python
# encoding: utf-8

# Copyright (C) Alibaba Cloud Computing
# All rights reserved.


import logging
import time
from enum import Enum

from elasticsearch import Elasticsearch

from aliyun.log import LogClient, PutLogsRequest
from aliyun.log.es_migration.doc_logitem_converter import DocLogItemConverter
from aliyun.log.es_migration.util import split_and_strip


def run_collection_task(config):
    start_time = time.time()
    try:
        task = CollectionTask(config.task_id, config.slice_id, config.slice_max, config.hosts, config.indexes,
                              config.query, config.scroll, config.endpoint, config.project, config.access_key_id,
                              config.access_key, config.logstore_index_mapper, config.time_reference, config.source,
                              config.topic, start_time)
        return task.collect()
    except Exception as e:
        logging.exception("Failed to run collection task. e=%s", e)
        return build_collection_task_result(config.task_id, config.slice_id, config.slice_max, config.hosts,
                                            config.indexes, config.query, config.project, start_time,
                                            CollectionTaskStatus.FAIL_NO_RETRY, str(e))


def build_collection_task_result(task_id, slice_id, slice_max, hosts, indexes, query, project, start_time, status,
                                 message=None):
    cur_time = time.time()
    time_cost_in_seconds = cur_time - start_time
    return CollectionTaskResult(task_id, slice_id, slice_max, hosts, indexes, query, project, time_cost_in_seconds,
                                status, message)


class CollectionTaskResult(object):

    def __init__(self, task_id, slice_id, slice_max, hosts, indexes, query, project, time_cost_in_seconds, status,
                 message=None):
        self.task_id = task_id
        self.slice_id = slice_id
        self.slice_max = slice_max
        self.hosts = hosts
        self.indexes = indexes
        self.query = query
        self.project = project
        self.time_cost_in_seconds = time_cost_in_seconds
        self.status = status
        self.message = message

    def __str__(self):
        return "task_id=%s, slice_id=%s, slice_max=%s, hosts=%s, indexes=%s, " \
               "query=%s, project=%s, time_cost_in_seconds=%s, status=%s, message=%s" % \
               (self.task_id, self.slice_id, self.slice_max, self.hosts, self.indexes, self.query, self.project,
                self.time_cost_in_seconds, self.status, self.message)


class CollectionTaskStatus(Enum):
    SUCCESS = "SUCCESS"
    FAIL_RETRY = "FAIL_RETRY"
    FAIL_NO_RETRY = "FAIL_NO_RETRY"


class CollectionTask(object):
    DEFAULT_SIZE = 1000

    def __init__(self, task_id, slice_id, slice_max, hosts, indexes, query, scroll, endpoint, project, access_key_id,
                 access_key, logstore_index_mapper, time_reference, source, topic, start_time):
        self.task_id = task_id
        self.slice_id = slice_id
        self.slice_max = slice_max
        self.hosts = hosts
        self.indexes = indexes
        self.query = query
        self.scroll = scroll
        self.project = project
        self.logstore_index_mapper = logstore_index_mapper
        self.time_reference = time_reference
        self.source = source
        self.topic = topic
        self.start_time = start_time
        self.log_client = LogClient(endpoint, access_key_id, access_key)

    def collect(self):
        hosts = split_and_strip(self.hosts, ",")
        es = Elasticsearch(hosts)

        query = self.query.copy() if self.query else {}
        query["sort"] = "_doc"

        # initial search
        resp = es.search(index=self.indexes, body=query, scroll=self.scroll, size=self.DEFAULT_SIZE)

        scroll_id = resp.get("_scroll_id")
        if scroll_id is None:
            return self._build_collection_task_result(CollectionTaskStatus.SUCCESS)

        try:
            first_run = True
            while True:
                # if we didn't set search_type to scan initial search contains data
                if first_run:
                    first_run = False
                else:
                    resp = es.scroll(scroll_id, scroll=self.scroll)

                self._write_docs_to_aliyun_log(resp["hits"])

                # check if we have any errrors
                if resp["_shards"]["successful"] < resp["_shards"]["total"]:
                    msg = "Scroll request has only succeeded on %d shards out of %d." % \
                          (resp["_shards"]["successful"], resp["_shards"]["total"])
                    logging.warning(msg)
                    return self._build_collection_task_result(CollectionTaskStatus.FAIL_NO_RETRY, msg)
                scroll_id = resp.get("scroll_id")
                # end of scroll
                if scroll_id is None or not resp["hits"]["hits"]:
                    break

            return self._build_collection_task_result(CollectionTaskStatus.SUCCESS)
        finally:
            if scroll_id:
                es.clear_scroll(body={"scroll_id": [scroll_id]}, ignore=(404,))

    def _build_collection_task_result(self, status, message=None):
        cur_time = time.time()
        time_cost_in_seconds = cur_time - self.start_time
        return CollectionTaskResult(self.task_id, self.slice_id, self.slice_max, self.hosts, self.indexes, self.query,
                                    self.project, time_cost_in_seconds, status, message)

    def _write_docs_to_aliyun_log(self, hits):
        if not hits["hits"]:
            return

        logstore_log_items_dct = {}
        for hit in hits["hits"]:
            index = DocLogItemConverter.get_index(hit)
            log_item = DocLogItemConverter.to_log_item(hit, self.time_reference)
            logstore = self.logstore_index_mapper.get_logstore(index) or index
            if logstore not in logstore_log_items_dct:
                logstore_log_items_dct[logstore] = []
            logstore_log_items_dct[logstore].append(log_item)

        source = self.source or self.hosts
        for logstore, log_item_lst in logstore_log_items_dct.iteritems():
            request = PutLogsRequest(self.project, logstore, self.topic, source, log_item_lst)
            self.log_client.put_logs(request)
