import os
from typing import List
import requests
import json
import yaml
import rich
import logging

from dataclasses import dataclass
from packaging.version import parse as parse_version
from pathlib import Path
from dbt_artifacts_parser.parser import parse_run_results, parse_manifest
from . import connect_to_table, diff_tables, Algorithm
from dbt.config.renderer import ProfileRenderer

RUN_RESULTS_PATH = "/target/run_results.json"
MANIFEST_PATH = "/target/manifest.json"
PROJECT_FILE = "/dbt_project.yml"
PROFILES_FILE = "/profiles.yml"
DBT_V_ABOVE = "1.0.0"
DBT_V_BELOW = "1.4.0"


@dataclass
class DiffVars:
    dev_path: List[str]
    prod_path: List[str]
    primary_keys: List[str]
    datasource_id: str
    connection: dict


class DbtDiffer:
    @classmethod
    def diff(self, profiles_dir_override: str = None, project_dir_override: str = None, is_cloud: bool = False):
        dbt_parser = DbtParser(profiles_dir_override, project_dir_override, is_cloud)
        models = dbt_parser.get_models()
        dbt_parser.set_project_dict()
        datadiff_variables = dbt_parser.get_datadiff_variables()
        config_prod_database = datadiff_variables.get("prod_database")
        config_prod_schema = datadiff_variables.get("prod_schema")
        datasource_id = datadiff_variables.get("datasource_id")

        if not is_cloud:
            dbt_parser.set_connection()

        if config_prod_database is None or config_prod_schema is None:
            raise ValueError("Expected a value for prod_database: or prod_schema: under \nvars:\n  data_diff: ")

        for model in models:
            diff_vars = self._get_diff_vars(dbt_parser, config_prod_database, config_prod_schema, model, datasource_id)

            if is_cloud and len(diff_vars.primary_keys) > 0:
                self._cloud_diff(diff_vars)
            elif is_cloud:
                rich.print(
                    "[red]"
                    + ".".join(diff_vars.dev_path)
                    + " <> "
                    + ".".join(diff_vars.prod_path)
                    + "[/] \n"
                    + "Skipped due to missing primary-key tag\n"
                )

            if not is_cloud and len(diff_vars.primary_keys) == 1:
                self._local_diff(diff_vars)
            elif not is_cloud:
                rich.print(
                    "[red]"
                    + ".".join(diff_vars.dev_path)
                    + " <> "
                    + ".".join(diff_vars.prod_path)
                    + "[/] \n"
                    + "Skipped due to missing primary-key tag or multi-column primary-key (unsupported for non --cloud diffs)\n"
                )

        rich.print("Diffs Complete!")

    @staticmethod
    def _get_diff_vars(dbt_parser, config_prod_database, config_prod_schema, model, datasource_id) -> DiffVars:
        dev_database = model.database
        dev_schema = model.schema_
        primary_keys = dbt_parser.get_primary_keys(model)

        if config_prod_database is None:
            prod_database = dev_database
        else:
            prod_database = config_prod_database

        if config_prod_schema is None:
            prod_schema = dev_schema
        else:
            prod_schema = config_prod_schema

        if dbt_parser.requires_upper:
            dev_qualified_list = [x.upper() for x in [dev_database, dev_schema, model.name]]
            prod_qualified_list = [x.upper() for x in [prod_database, prod_schema, model.name]]
            primary_keys = [x.upper() for x in primary_keys]
        else:
            dev_qualified_list = [dev_database, dev_schema, model.name]
            prod_qualified_list = [prod_database, prod_schema, model.name]
        return DiffVars(dev_qualified_list, prod_qualified_list, primary_keys, datasource_id, dbt_parser.connection)

    @staticmethod
    def _local_diff(diff_vars: DiffVars):
        column_diffs_str = ""
        dev_qualified_string = ".".join(diff_vars.dev_path)
        prod_qualified_string = ".".join(diff_vars.prod_path)
        primary_key = diff_vars.primary_keys[0]

        table1 = connect_to_table(diff_vars.connection, dev_qualified_string, primary_key)
        table2 = connect_to_table(diff_vars.connection, prod_qualified_string, primary_key)

        table1_columns = list(table1.get_schema())
        try:
            table2_columns = list(table2.get_schema())
        # TODO add a unit test for scenario
        except Exception as e:
            logging.debug(e)
            rich.print(
                "[red]"
                + dev_qualified_string
                + " <> "
                + prod_qualified_string
                + "[/] \n"
                + column_diffs_str
                + "[green]New model or no access to prod table.[/] \n"
            )
            return

        mutual_set = set(table1_columns) & set(table2_columns)
        table1_set_diff = list(set(table1_columns) - set(table2_columns))
        table2_set_diff = list(set(table2_columns) - set(table1_columns))

        if len(table1_set_diff) > 0:
            column_diffs_str += f"Columns exclusive to table A: {table1_set_diff}\n"

        if len(table2_set_diff) > 0:
            column_diffs_str += f"Columns exclusive to table B: {table2_set_diff}\n"

        mutual_set.discard(primary_key)
        extra_columns = tuple(mutual_set)

        diff = diff_tables(table1, table2, threaded=True, algorithm=Algorithm.JOINDIFF, extra_columns=extra_columns)

        if len(list(diff)) > 0:
            rich.print(
                "[red]"
                + dev_qualified_string
                + " <> "
                + prod_qualified_string
                + "[/] \n"
                + column_diffs_str
                + diff.get_stats_string()
                + "\n"
            )
        else:
            rich.print(
                "[red]"
                + dev_qualified_string
                + " <> "
                + prod_qualified_string
                + "[/] \n"
                + column_diffs_str
                + "[green]No row differences[/] \n"
            )

    @staticmethod
    def _cloud_diff(diff_vars: DiffVars):
        api_key = os.environ.get("DATAFOLD_API_KEY")

        if diff_vars.datasource_id is None:
            raise ValueError(
                "Datasource ID not found, include it as a dbt variable in the dbt_project.yml. \nvars:\n data_diff:\n   datasource_id: 1234"
            )
        elif api_key is None:
            raise ValueError("API key not found, add it as an environment variable called DATAFOLD_API_KEY.")
        else:
            url = "https://app.datafold.com/api/v1/datadiffs"

            payload = json.dumps(
                {
                    "data_source1_id": diff_vars.datasource_id,
                    "data_source2_id": diff_vars.datasource_id,
                    "table1": diff_vars.dev_path,
                    "table2": diff_vars.prod_path,
                    "pk_columns": diff_vars.primary_keys,
                }
            )
            headers = {
                "Authorization": "Key " + api_key,
                "Content-Type": "application/json",
            }

            response = requests.request("POST", url, headers=headers, data=payload)
            response.raise_for_status()
            data = response.json()
            id = data["id"]
            diff_url = f"https://app.datafold.com/datadiffs/{id}/overview"
            rich.print(
                "[red]"
                + ".".join(diff_vars.dev_path)
                + " <> "
                + ".".join(diff_vars.prod_path)
                + "[/] \n    Diff in progress: \n    "
                + diff_url
                + "\n"
            )


class DbtParser:
    DEFAULT_PROFILES_DIR = str(Path.home()) + "/.dbt"
    DEFAULT_PROJECT_DIR = str(os.getcwd())

    def __init__(self, profiles_dir_override: str, project_dir_override: str, is_cloud: bool):
        self.profiles_dir = profiles_dir_override or self.DEFAULT_PROFILES_DIR
        self.project_dir = project_dir_override or self.DEFAULT_PROJECT_DIR
        self.is_cloud = is_cloud
        self.connection = None
        self.project_dict = None
        self.requires_upper = False

    def get_datadiff_variables(self):
        try:
            vars = self.project_dict.get("vars").get("data_diff")
            assert vars is not None
        except Exception as e:
            logging.debug(e)
            raise (Exception("Expected values in dbt_project.yml under \nvars:\n  data_diff:"))
        return vars

    def get_models(self):
        with open(self.project_dir + RUN_RESULTS_PATH) as run_results:
            run_results_dict = json.load(run_results)
            run_results_obj = parse_run_results(run_results=run_results_dict)

        # TODO need test for less than scenario
        dbt_version = parse_version(run_results_obj.metadata.dbt_version)
        assert dbt_version >= parse_version(
            "1.0.0"
        ) and dbt_version < parse_version(
            "1.4"
        ), f"Found dbt: v{dbt_version} Expected the dbt project's version to be >= {DBT_V_ABOVE} and < {DBT_V_BELOW}"

        with open(self.project_dir + MANIFEST_PATH) as manifest:
            manifest_dict = json.load(manifest)
            manifest_obj = parse_manifest(manifest=manifest_dict)

        success_models = [x.unique_id for x in run_results_obj.results if x.status.name == "success"]
        models = [manifest_obj.nodes.get(x) for x in success_models]
        assert len(models) > 0, "Expected > 0 successful models runs from the last dbt command."
        rich.print(f"Found {str(len(models))} successful model runs from the last dbt command.")
        return models

    def get_primary_keys(self, model):
        return list((x.name for x in model.columns.values() if "primary-key" in x.tags))

    def set_project_dict(self):
        with open(self.project_dir + PROJECT_FILE) as project:
            self.project_dict = yaml.safe_load(project)

    def set_connection(self):
        with open(self.profiles_dir + PROFILES_FILE) as profiles:
            profiles = yaml.safe_load(profiles)

        try:
            dbt_profile = self.project_dict.get("profile")
            profile_outputs = profiles.get(dbt_profile)
            profile_target = profile_outputs.get("target")
            credentials = profile_outputs.get("outputs").get(profile_target)
            conn_type = credentials.get("type").lower()

            # values can contain env_vars
            rendered_credentials = ProfileRenderer().render_data(credentials)
        except Exception as e:
            raise (
                Exception(
                    f"Failed to find credentials in {self.profiles_dir + '/profiles.yml'} for the profile specified in {self.project_dir + '/dbt_project.yml'}."
                    + str(e)
                )
            )

        if conn_type == "snowflake":
            assert (
                rendered_credentials.get("password") is not None
                and rendered_credentials.get("private_key_path") is None
            ), "Only password authentication is currently supported for Snowflake."
            conn_info = {
                "driver": conn_type,
                "user": rendered_credentials.get("user"),
                "password": rendered_credentials.get("password"),
                "account": rendered_credentials.get("account"),
                "database": rendered_credentials.get("database"),
                "warehouse": rendered_credentials.get("warehouse"),
                "role": rendered_credentials.get("role"),
                "schema": rendered_credentials.get("schema"),
            }
            self.requires_upper = True
        elif conn_type == "bigquery":
            method = rendered_credentials.get("method")
            # there are many connection types https://docs.getdbt.com/reference/warehouse-setups/bigquery-setup#oauth-via-gcloud
            # this assumes that the user is auth'd via `gcloud auth application-default login`
            assert method is not None and method == "oauth", "Oauth is the current method supported for Big Query."
            conn_info = {
                "driver": conn_type,
                "project": rendered_credentials.get("project"),
                "dataset": rendered_credentials.get("dataset"),
            }
        else:
            raise NotImplementedError(f"Provider {conn_type} is not yet supported for dbt diffs")

        self.connection = conn_info
