"""
Copyright 2023 Google LLC

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

     https://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License."""

"""Utility functions for DAGs."""

from collections import defaultdict
import importlib
import os
import errors
import pathlib
import sys
import re
import hashlib
import requests
import traceback
from dataclasses import dataclass, field
from typing import Any, List, Dict, Mapping, Sequence, Tuple
import time
import uuid

import cloud_detect
import yaml

import tadau

from airflow.providers.apache.drill.hooks.drill import DrillHook
from pydantic import BaseModel
from google.ads.googleads.client import GoogleAdsClient

_TABLE_ALIAS = "t"
_DRILL_ADDRESS = "http://drill:8047"

_DEFAULT_GOOGLE_ADS_API_VERSION = "v17"
_REQUIRED_GOOGLE_ADS_CREDENTIALS = frozenset([
  "client_id",
  "client_secret",
  "developer_token",
  "login_customer_id",
  "refresh_token"])


@dataclass
class ProtocolSchema:
  """Class that defines the schema of a source or destination protocol."""

  class_name: str
  fields: Sequence[Tuple[str, type] | Tuple[str, type, field]]


@dataclass
class ValidationResult:
  """Class for reporting of validation results."""

  is_valid: bool
  messages: Sequence[str]


@dataclass
class RunResult:
  """Class for reporting the result of a DAG run."""

  successful_hits: int = 0
  failed_hits: int = 0
  error_messages: Sequence[str] = field(default_factory=lambda: [])
  dry_run: bool = False

  def __add__(self, other: "RunResult") -> "RunResult":
    sh = self.successful_hits + other.successful_hits
    fh = self.failed_hits + other.failed_hits
    em = self.error_messages + other.error_messages
    dr = self.dry_run or other.dry_run
    return RunResult(sh, fh, em, dr)


class SchemaUtils:
  """A set of utility functions for defining schemas."""

  @staticmethod
  def key_value_type():
    class KeyValue(BaseModel):
      key: str
      value: str
    return KeyValue

  @staticmethod
  def raw_json_type():
    class RawJSON(BaseModel):
      value: str
    return RawJSON


class DagUtils:
  """A set of utility functions for DAGs."""

  def import_modules_from_folder(self, folder_name: str):
    """Import all modules from a given folder."""
    modules = []
    dags_path = f"airflow/dags/{folder_name}"
    folder_path = pathlib.Path().resolve().parent / dags_path
    for filename in os.listdir(folder_path):
      if os.path.isfile(folder_path / filename) and filename != "__init__.py":
        module_name, _ = filename.split(".py")
        module_path = os.path.join(folder_path, filename)
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        modules.append(module)
    return modules


class GoogleAdsUtils:
  """Utility functions for Google Ads connectors."""
  PartialFailures = Dict[int, str]

  def validate_google_ads_config(self, config: dict[str, Any]) -> ValidationResult:
    """Validates the provided config can build a Google Ads client.

    Args:
      config: The Tightlock config file.

    Returns:
      A ValidationResult for the provided config.
    """
    missing_fields = []
    for credential in _REQUIRED_GOOGLE_ADS_CREDENTIALS:
      if not config.get(credential, ""):
        missing_fields.append(credential)

    if missing_fields:
      error_msg = (
        "Config requires the following fields to be set: "
        f"{', '.join(missing_fields)}")
      return ValidationResult(False, [error_msg])

    return ValidationResult(True, [])

  def build_google_ads_client(
      self,
      config: dict[str, Any],
      version: str=_DEFAULT_GOOGLE_ADS_API_VERSION) -> GoogleAdsClient:
    """Generate Google Ads Client.

    Requires the following to be stored in config:
    - client_id
    - client_secret
    - developer_token
    - login_customer_id
    - refresh_token

    Args:
      config: The Tightlock config file.
      version: (Optional) Version number for Google Ads API prefixed with v.

    Returns: Instance of GoogleAdsClient
    """
    credentials = {}

    for credential in _REQUIRED_GOOGLE_ADS_CREDENTIALS:
      credentials[credential] = config.get(credential, "")

    credentials["use_proto_plus"] = True

    return GoogleAdsClient.load_from_dict(
      config_dict=credentials, version=version)

  def get_partial_failures(self, client: GoogleAdsClient, response: Any) -> PartialFailures:
    """Checks whether a response message has a partial failure error.

    In Python the partial_failure_error attr is always present on a response
    message and is represented by a google.rpc.Status message. So we can't
    simply check whether the field is present, we must check that the code is
    non-zero. Error codes are represented by the google.rpc.Code proto Enum:
    https://github.com/googleapis/googleapis/blob/master/google/rpc/code.proto

    Args:
        response:  A MutateAdGroupsResponse message instance.

    Returns: An empty dict if no partial failures exist, or a dict of the index
      index mapped to the error message.
    """
    partial_failure = getattr(response, "partial_failure_error", None)
    code = getattr(partial_failure, "code", None)
    if code == 0:
      # No failures.
      print("No partial failures found.")
      return {}

    error_details = getattr(partial_failure, "details", [])

    partial_failures = defaultdict(str)

    for error_detail in error_details:
      # Retrieve an instance of the GoogleAdsFailure class from the client
      failure_message = client.get_type("GoogleAdsFailure")
      # Parse the string into a GoogleAdsFailure message instance.
      # To access class-only methods on the message we retrieve its type.
      GoogleAdsFailure = type(failure_message)
      failure_object = GoogleAdsFailure.deserialize(error_detail.value)

      for error in failure_object.errors:
        index = error.location.field_path_elements[0].index
        message = f'Code: {error.error_code}, Error: {error.message}'
        partial_failures[index] += message  # Can be multiple errors for the same conversion.

    print(f"Partial failures: {partial_failures}")

    return partial_failures

  def normalize_and_hash_email_address(self, email_address: str) -> str:
    """Returns the result of normalizing and hashing an email address.

    For this use case, Google Ads requires removal of any '.' characters
    preceding "gmail.com" or "googlemail.com"

    Args:
        email_address: An email address to normalize.

    Returns:
        A normalized (lowercase, removed whitespace) and SHA-265 hashed string.
    """
    normalized_email = email_address.lower()
    email_parts = normalized_email.split("@")
    # Checks whether the domain of the email address is either "gmail.com"
    # or "googlemail.com". If this regex does not match then this statement
    # will evaluate to None.
    is_gmail = re.match(r"^(gmail|googlemail)\.com$", email_parts[1])

    # Check that there are at least two segments and the second segment
    # matches the above regex expression validating the email domain name.
    if len(email_parts) > 1 and is_gmail:
        # Removes any '.' characters from the portion of the email address
        # before the domain if the domain is gmail.com or googlemail.com.
        email_parts[0] = email_parts[0].replace(".", "")
        normalized_email = "@".join(email_parts)

    return self.normalize_and_hash(normalized_email)


  def normalize_and_hash(self, s: str) -> str:
    """Normalizes and hashes a string with SHA-256.

    Private customer data must be hashed during upload, as described at:
    https://support.google.com/google-ads/answer/7474263

    Args:
        s: The string to perform this operation on.

    Returns:
        A normalized (lowercase, removed whitespace) and SHA-256 hashed string.
    """
    return hashlib.sha256(s.strip().lower().encode()).hexdigest()


class DrillMixin:
  """A Drill mixin that provides utils like a get_drill_data wrapper for other classes that use Drill."""

  def _validate_or_update_config_obj(
      self,
      obj: Mapping[str, Any],
      key: str,
      value: str) -> bool:
    """Checks if value of key is the same as target, updates otherwise.    

    Args:
      obj: the target object to be checked/updated
      key: the target key of the object
      value: the value that needs to be validated/updated

    Returns: A boolean indicating whether or not an update was required.
    """
    if not value:
      if key in obj:
        del obj[key]
      else:
        return False
    elif obj.get(key) == value:
      return False
    else:
      obj[key] = value

    return True

  def _get_storage(self, name: str) -> Mapping[str, Any]:
    endpoint = f"{_DRILL_ADDRESS}/storage/{name}.json"
    r = requests.get(endpoint)

    if r.status_code == 200:
      return r.json()

    raise errors.DataInConnectorError(
        f"Failed to connect to Drill: {r.json()['errorMessage']}"
    )

  def _set_storage(self, name: str, config: Mapping[str, Any]):
    endpoint = f"{_DRILL_ADDRESS}/storage/{name}.json"
    r = requests.post(endpoint, json=config)

    if r.status_code != 200:
      raise errors.DataOutConnectorError(
          f"Failed to connect to Drill: {r.text}"
      )
    else:
      print(f"Updating {name} Drill storage plugin.")

  def _parse_data(self, fields, rows: List[Tuple[str, ...]]) -> List[Mapping[str, Any]]:
    """Parses data and transforms it into a list of dictionaries."""
    events = []
    for event in rows:
      event_dict = {}
      # relies on Drill preserving the order of fields provided in the query
      for i, field in enumerate(fields):
        event_dict[field] = event[i]

      # ignore empty rows
      if any(event_dict.values()):
        events.append(event_dict)

    return events

  def get_drill_data(
      self,
      from_target: Sequence[str],
      fields: Sequence[str],
      offset: int,
      limit: int,
      unique_id: str
  ) -> List[Mapping[str, Any]]:
    drill_conn = DrillHook().get_conn()
    cursor = drill_conn.cursor()
    table_alias = _TABLE_ALIAS
    fields_str = ",".join([f"{table_alias}.{field}" for field in fields])
    query = (
        f"SELECT {fields_str}"
        f" FROM {from_target} as {table_alias}"
        f" ORDER BY {unique_id}"
        f" LIMIT {limit} OFFSET {offset}"
    )
    try:
      cursor.execute(query)
      results = self._parse_data(fields, cursor.fetchall())
    except RuntimeError:
      # Return an empty list when an empty cursor is fetched
      results = []
    return results

  def validate_drill(self, path: str, unique_id: str) -> ValidationResult:
    drill_conn = DrillHook().get_conn()
    cursor = drill_conn.cursor()

    # validates Drill engine is working and path is reachable
    try:
      query = f"SELECT {unique_id} FROM {path}"
      cursor.execute(query)

      # validates unique_id existance
      id_value = cursor.fetchone()[0]

      if not id_value:
        return ValidationResult(False, [f"Column {unique_id} could not be find in {path}."])
   
    except Exception:  # pylint: disable=broad-except
      return ValidationResult(False, [f"Error validation location `{path}`: {traceback.format_exc()}"])
   
    return ValidationResult(True, [])


class TadauMixin:
  """A data usage collection Mixin that uses the Tadau lib and can be used by destinations."""

  def __init__(self):

    # setup Tadau library for data collection if consent was provided
    collection_consent = os.environ.get(
        "USAGE_COLLECTION_ALLOWED", False)
    api_secret = os.environ.get(
        "TADAU_API_SECRET"
    )
    measurement_id = os.environ.get(
        "TADAU_MEASUREMENT_ID"
    )

    self._tadau = None
    tadau_path = "airflow/dags/tadau"
    folder_path = pathlib.Path().resolve().parent / tadau_path
    file_path = f"{folder_path}/config.yaml"

    if collection_consent and not os.path.exists(folder_path):
      # config file init if instantiate for the first time
      mode = 0o777
      try:
        os.makedirs(folder_path, mode)
      except FileExistsError:
        # skips folder creation
        pass

      with open(file_path, "w") as f:
        y = {}
        y["fixed_dimensions"] = {}
        y["fixed_dimensions"]["deploy_id"] = f"tightlock_{str(uuid.uuid4())}"
        y["fixed_dimensions"]["deploy_infra"] = cloud_detect.provider()
        y["fixed_dimensions"]["deploy_created_time"] = time.time()
        y["api_secret"] = api_secret
        y["measurement_id"] = measurement_id
        y["opt_in"] = collection_consent

        yaml.dump(data=y, stream=f)
      
    try:
      self._tadau = tadau.Tadau(config_file_location=file_path)
    except AssertionError:
      # if no consent was given, Tadau will raise an AssertionError  
      self._tadau = None

  @property
  def tadau(self) -> tadau.Tadau | None:
    return self._tadau


