// Copyright (C) 2016 iNuron NV
//
// This file is part of Open vStorage Open Source Edition (OSE),
// as available from
//
//      http://www.openvstorage.org and
//      http://www.openvstorage.com.
//
// This file is free software; you can redistribute it and/or modify it
// under the terms of the GNU Affero General Public License v3 (GNU AGPLv3)
// as published by the Free Software Foundation, in version 3 as it comes
// in the LICENSE.txt file of the Open vStorage OSE distribution.
//
// Open vStorage is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY of any kind.
/*global define */
define([
    'jquery',
    'ovs/shared', 'ovs/generic', 'ovs/api'
], function($, shared, generic, api) {
    "use strict";
    return function() {
        var self = this;

        self.shared      = shared;
        self.hooks       = {};
        self.taskIDQueue = [];

        self.wait = function(taskID) {
            var i;
            for (i = 0; i < self.taskIDQueue.length; i += 1){
                if (self.taskIDQueue[i].id === taskID) {
                    return self.load(taskID);
                }
            }
            self.hooks[taskID] = $.Deferred();
            return self.hooks[taskID].promise();
        };
        self.load = function(taskID) {
            return $.Deferred(function(deferred) {
                api.get('tasks/' + taskID)
                    .done(function(data) {
                        if (data.successful === true) {
                            deferred.resolve(data.result);
                        } else {
                            deferred.reject(data.result);
                        }
                    })
                    .fail(function(error) {
                        deferred.reject(error);
                    });
            }).promise();
        };
        self.validateTasks = function() {
            $.each(self.hooks, function(taskID, deferred) {
                api.get('tasks/' + taskID)
                    .done(function(data) {
                        if (data.ready === true) {
                            if (data.successful === true) {
                                deferred.resolve(data.result);
                            } else {
                                deferred.reject(data.result);
                            }
                        }
                    });
            });
        };

        self.shared.messaging.subscribe('TASK_COMPLETE', function(taskID) {
            if (self.hooks.hasOwnProperty(taskID)) {
                self.load(taskID)
                    .done(self.hooks[taskID].resolve)
                    .fail(self.hooks[taskID].reject);
            } else {
                var now = generic.getTimestamp(), i, newQueue = [];
                self.taskIDQueue.push({ id: taskID, timestamp: now });
                for (i = 0; i < self.taskIDQueue.length; i += 1) {
                    if (self.taskIDQueue[i].timestamp >= now - 10000) {
                        newQueue.push(self.taskIDQueue[i]);
                    }
                }
                self.taskIDQueue = newQueue;
            }
        });
    };
});
