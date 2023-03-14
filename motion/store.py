import dill
import duckdb
import inspect
import logging
import os
import pandas as pd
import typing

from collections import namedtuple
from croniter import croniter
from enum import Enum
from motion import Trigger
from motion.dbcon import Connection
from motion.task import TaskThread
from motion.trigger import TriggerElement, TriggerFn

CONNECTIONS = {}


def get_store(name: str, create: bool, memory: bool = True) -> typing.Any:
    """Get or create a store with the given name.

    Args:
        name (str): The name of the store to get or create.
        create (bool): Whether to create the store if it doesn't exist.
        memory (bool, optional): Whether to use memory for the store. Defaults to True.

    Returns:
        typing.Any: The store.
    """
    if name not in CONNECTIONS:
        if not create:
            raise Exception(f"Store {name} does not exist. Set create=True.")

        CONNECTIONS[name] = Store(name, memory=memory)

    return CONNECTIONS[name]


class Store(object):
    def __init__(
        self,
        name: str,
        memory: bool = True,
        datastore_prefix: str = "datastores",
    ):
        self.name = name
        self.memory = memory

        if not memory and not os.path.exists(
            os.path.join(datastore_prefix, name)
        ):
            os.makedirs(os.path.join(datastore_prefix, name))

        self.con = (
            duckdb.connect(":memory:")
            if self.memory
            else duckdb.connect(
                os.path.join(datastore_prefix, name, "duck.db")
            )
        )

        self.con.execute(f"CREATE SCHEMA IF NOT EXISTS {name}")
        self.addLogTable()

        self.triggers = {}
        self.cron_triggers = {}  # TODO(figure out how to schedule this)
        self.trigger_names = {}
        self.trigger_fns = {}

        self.table_columns = (
            dill.load(
                open(
                    os.path.join(datastore_prefix, name, "table_columns"), "rb"
                )
            )
            if os.path.exists(
                os.path.join(datastore_prefix, name, "table_columns")
            )
            else {}
        )

        self.datastore_prefix = datastore_prefix

        # Set listening to false
        self._listening = False

    @property
    def listening(self):
        return self._listening

    def cursor(self):
        """Generates a new cursor for the database, with triggers and all.

        Returns:
            Connection: The cursor.
        """
        if not self.listening:
            raise Exception(
                "Store has not started. Call store.start() before using the cursor."
            )

        return Connection(
            self.name, self.con, self.table_columns, self.triggers
        )

    def addLogTable(self):
        """Creates a table to store trigger logs."""

        self.con.execute(
            f"CREATE TABLE IF NOT EXISTS {self.name}.logs(executed_time DATETIME DEFAULT CURRENT_TIMESTAMP, trigger_name VARCHAR, trigger_version INTEGER, trigger_action VARCHAR, namespace VARCHAR, id INTEGER, trigger_key VARCHAR)"
        )

    def addNamespace(self, name: str, schema: typing.Any) -> None:
        """Add a namespace to the store.

        Args:
            name (str): The name of the namespace.
            schema (typing.Any): The schema of the namespace.
        """

        # Check if namespace already exists
        tables = self.con.execute(f"SHOW TABLES;").fetchall()
        tables = [t[0] for t in tables]
        if name in tables:
            logging.warning(
                f"Namespace {name} already exists in store {self.name}. Doing nothing."
            )
            return

        stmts = schema.formatCreateStmts(f"{self.name}.{name}")
        for stmt in stmts:
            logging.info(stmt)
            self.con.execute(stmt)

        # Create sequence for id
        self.con.execute(f"CREATE SEQUENCE {self.name}.{name}_id_seq;")

        # Store column names
        self.table_columns[name] = (
            self.con.execute(f"DESCRIBE {self.name}.{name};")
            .fetchdf()["column_name"]
            .tolist()
        )
        self.table_columns[name].remove("id")
        self.table_columns[name].remove("derived_id")

        # Persist
        if not self.memory:
            dill.dump(
                self.table_columns,
                open(
                    os.path.join(self.datastore_prefix, name, "table_columns"),
                    "wb",
                ),
            )

    def deleteNamespace(self, name: str) -> None:
        """Delete a namespace from the store.
        TODO(shreya): Error checking

        Args:
            name (str): The name of the namespace.
        """
        self.con.execute(f"DROP TABLE {self.name}.{name};")
        self.con.execute(f"DROP SEQUENCE {self.name}.{name}_id_seq;")

        # Remove column names
        del self.table_columns[name]

        # Persist
        if not self.memory:
            dill.dump(
                self.table_columns,
                open(
                    os.path.join(self.datastore_prefix, name, "table_columns"),
                    "wb",
                ),
            )

    def addTrigger(
        self,
        name: str,
        keys: typing.List[str],
        trigger: typing.Union[typing.Callable, type],
    ) -> None:
        """Adds a trigger to the store.

        Args:
            name (str): Trigger name.
            keys (typing.List[str]): Names of the keys to triger on. Formatted
            as "namespace.key" or cron expression. Trigger executes if there is
            a addition to any of the keys, or on the cron schedule.
            trigger (typing.Union[typing.Callable, type]): Function or class to
            execute when the trigger is fired. If function, must take in the id
            of the row that triggered the trigger, a reference to the element
            that triggered it, and a reference to the store object (in this
            order). If class, must implement the Transform interface.

        Raises:
            ValueError: If there is already a trigger with the given name.
        """
        if name in self.trigger_names:
            logging.warning(f"Trigger {name} already exists. Doing nothing.")
            return

        if inspect.isfunction(trigger):
            # Check that the function signature is correct
            if len(inspect.signature(trigger).parameters) != 3:
                raise ValueError(
                    f"Trigger function must take in 3 arguments: store connection, id, and triggered_by."
                )

        elif inspect.isclass(trigger):
            # Check that the class implements the Transform interface
            if not issubclass(trigger, Trigger):
                raise ValueError(
                    f"Trigger class must implement the Trigger interface."
                )

        else:
            raise ValueError(
                f"Trigger {name} must be a function or class. Got {type(trigger)}."
            )

        # Check that keys are valid
        all_possible_keys = [
            f"{ns}.{key}"
            for ns in self.table_columns
            for key in self.table_columns[ns]
        ]
        for key in keys:
            if key not in all_possible_keys and not croniter.is_valid(key):
                raise ValueError(
                    f"Trigger {name} has invalid key {key}. Valid keys are {all_possible_keys} or a cron expression."
                )

        # Add the trigger to the store
        self.trigger_names[name] = keys

        version = self.con.execute(
            f"SELECT MAX(trigger_version) FROM {self.name}.logs WHERE trigger_name = '{name}';"
        ).fetchone()
        version = version[0] if version[0] else 0
        trigger_exec = (
            trigger(self.cursor(), name, version)
            if inspect.isclass(trigger)
            else trigger
        )
        self.trigger_fns[name] = trigger_exec

        for key in keys:
            if croniter.is_valid(key):
                self.cron_triggers[key] = self.cron_triggers.get(key, []) + [
                    TriggerFn(name, trigger_exec, inspect.isclass(trigger))
                ]

            else:
                self.triggers[key] = self.triggers.get(key, []) + [
                    TriggerFn(name, trigger_exec, inspect.isclass(trigger))
                ]

    def deleteTrigger(self, name: str) -> None:
        """Delete a trigger from the store.

        Args:
            name (str): The name of the trigger.
        """
        if name not in self.trigger_names:
            raise ValueError(f"Trigger {name} does not exist.")

        # Remove the trigger from the store
        keys = self.trigger_names[name]
        fn = self.trigger_fns[name]
        for key in keys:
            if croniter.is_valid(key):
                self.cron_triggers[key].remove(
                    (name, fn, isinstance(fn, Trigger))
                )
            else:
                self.triggers[key].remove((name, fn, isinstance(fn, Trigger)))
        del self.trigger_names[name]
        del self.trigger_fns[name]

    def getTriggersForKey(self, namespace: str, key: str) -> typing.List[str]:
        """Get the list of triggers for a given key.

        Args:
            namespace (str): The namespace to get the triggers for.
            key (str): The key to get the triggers for.

        Returns:
            typing.List[str]: The list of triggers for the given key.
        """
        names_and_fns = self.triggers.get(f"{namespace}.{key}", [])
        return [t[0] for t in names_and_fns]

    def getTriggersForAllKeys(self) -> typing.Dict[str, typing.List[str]]:
        """Get the list of triggers for all keys.

        Returns:
            typing.Dict[str, typing.List[str]]: The list of triggers for all keys.
        """
        return {k: self.getTriggersForKey(k) for k in self.triggers.keys()}

    def start(self) -> None:
        """Start the store."""
        # Start cron triggers
        self._listening = True
        self.cron_threads = {}

        for cron_expression, triggers in self.cron_triggers.items():
            self.cron_threads[cron_expression] = []
            for trigger_fn in triggers:
                t = TaskThread(cron_expression, self.cursor(), trigger_fn)
                self.cron_threads[cron_expression].append(t)
                t.start()

    def stop(self) -> None:
        """Stop the store."""
        # Stop cron triggers
        for _, threads in self.cron_threads.items():
            for t in threads:
                t.stop()
                t.join()

        self._listening = False
