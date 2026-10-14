# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The one rule that keeps schemas out of discovery (``database.schema_filter``).

Pins: the system schemas are excluded whatever their letter case (SQL Server's
``INFORMATION_SCHEMA`` included) and by exact name, so ``db_sales`` survives;
the patterns of the system list, the ``EXCLUDE_SCHEMAS`` setting, a source's
``exclude_schemas:`` and a command line compose in that order; and
``datasources.yaml`` validates the new key.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import config as cfg
from database.datasources import (
    DataSourceDefinition,
    datasource_exclude_schemas,
    reset_datasources_cache,
    validate_datasources_yaml_text,
)
from database.schema_filter import (
    RULE_CLI,
    RULE_CONFIGURED,
    RULE_SYSTEM,
    SYSTEM_SCHEMAS,
    SchemaExclusion,
    exclusion_patterns,
    is_system_schema,
    make_schema_filter,
    parse_patterns,
    schema_exclusion,
    schema_is_excluded,
)
from database.schema_inspector import schema_is_excluded as reexported


@pytest.fixture(autouse=True)
def _fresh_datasources():
    reset_datasources_cache()
    yield
    reset_datasources_cache()


def _project(tmp_path: Path, text: str | None) -> Path:
    if text is not None:
        (tmp_path / "datasources.yaml").write_text(text, encoding="utf-8")
    return tmp_path


TWO_SOURCES = """\
default: sales
datasources:
  sales:
    url_env: DB_URL_SALES
    exclude_schemas: ["*_Tmp", Scratch]
  reports:
    url_env: DB_URL_REPORTS
"""


class TestSystemSchemas:
    @pytest.mark.parametrize("name", ["sys", "SYS", "INFORMATION_SCHEMA", "information_schema", "Information_Schema"])
    def test_catalogue_schemas_in_any_case(self, name):
        assert is_system_schema(name)

    @pytest.mark.parametrize("name", ["guest", "Guest", "cdc", "CDC", "pg_catalog", "pg_toast", "pg_temp_1"])
    def test_guest_cdc_and_postgres_schemas(self, name):
        assert is_system_schema(name)

    @pytest.mark.parametrize("name", [
        "db_owner", "db_accessadmin", "db_securityadmin", "db_ddladmin", "db_backupoperator",
        "db_datareader", "db_datawriter", "db_denydatareader", "db_denydatawriter", "DB_OWNER",
    ])
    def test_the_nine_fixed_role_schemas(self, name):
        assert is_system_schema(name)

    @pytest.mark.parametrize("name", ["db_sales", "db_", "dbo", "sales", "cdc_archive", "sys_extra", "guests"])
    def test_user_schemas_with_look_alike_names_are_kept(self, name):
        assert not is_system_schema(name)

    def test_the_list_has_the_four_names_the_nine_roles_and_the_postgres_ones(self):
        lowered = {s.lower() for s in SYSTEM_SCHEMAS}
        assert {"sys", "information_schema", "guest", "cdc", "pg_catalog", "pg_toast", "pg_*"} <= lowered
        assert len([s for s in lowered if s.startswith("db_")]) == 9


class TestPatterns:
    def test_matching_is_case_insensitive_and_whole_name(self):
        assert schema_is_excluded("Sales_Stg", ["*_stg"])
        assert schema_is_excluded("REPLAY", ["Replay"])
        assert not schema_is_excluded("Replayed", ["Replay"])
        assert not schema_is_excluded("sales", None)

    def test_the_old_import_path_still_works(self):
        assert reexported is schema_is_excluded

    def test_parse_patterns_trims_and_drops_blanks(self):
        assert parse_patterns(" *_Stg,, Replay ,") == ("*_Stg", "Replay")
        assert parse_patterns(None) == () == parse_patterns("")


class TestComposition:
    def test_system_then_global_then_source_then_cli(self, tmp_path):
        env = _project(tmp_path, TWO_SOURCES)
        with cfg.override_settings(project_config_dir=str(env), exclude_schemas="Audit*, Legacy"):
            patterns = exclusion_patterns("sales", ["*_Stg"])

        assert patterns[: len(SYSTEM_SCHEMAS)] == SYSTEM_SCHEMAS
        assert patterns[len(SYSTEM_SCHEMAS):] == ("Audit*", "Legacy", "*_Tmp", "Scratch", "*_Stg")

    def test_another_source_does_not_get_the_first_ones_patterns(self, tmp_path):
        env = _project(tmp_path, TWO_SOURCES)
        with cfg.override_settings(project_config_dir=str(env), exclude_schemas="Audit*"):
            reports = exclusion_patterns("reports")

        assert "Scratch" not in reports and "*_Tmp" not in reports and "Audit*" in reports

    def test_without_datasources_yaml_the_global_setting_applies_to_the_default_source(self, tmp_path):
        with cfg.override_settings(project_config_dir=str(_project(tmp_path, None)), exclude_schemas="Audit*"):
            predicate = make_schema_filter("default")

        assert predicate("audit_2024") and predicate("cdc") and not predicate("sales")

    def test_no_source_means_only_system_and_global(self, tmp_path):
        env = _project(tmp_path, TWO_SOURCES)
        with cfg.override_settings(project_config_dir=str(env), exclude_schemas=""):
            predicate = make_schema_filter(None)

        assert predicate("sys") and not predicate("Scratch")

    def test_repeats_are_dropped_case_insensitively(self, tmp_path):
        with cfg.override_settings(project_config_dir=str(_project(tmp_path, None)), exclude_schemas="CDC, Audit"):
            patterns = exclusion_patterns("default", ["audit", "Extra"])

        lowered = [p.lower() for p in patterns]
        assert lowered.count("audit") == 1 and lowered.count("cdc") == 1 and "extra" in lowered

    def test_an_invalid_datasources_yaml_is_an_error(self, tmp_path):
        env = _project(tmp_path, "datasources:\n  a:\n    url_env: X\n    exclude_schemas: nope\n")
        with cfg.override_settings(project_config_dir=str(env)), pytest.raises(ValueError, match="exclude_schemas"):
            exclusion_patterns("a")

    def test_reason_attributes_a_schema_to_the_first_rule(self):
        rule = SchemaExclusion(configured=("Scratch*",), cli=("*_Tmp", "scratch_cli"))

        assert rule.reason("INFORMATION_SCHEMA") == RULE_SYSTEM
        assert rule.reason("scratch_cli") == RULE_CONFIGURED       # configured is tried before the command line
        assert rule.reason("load_tmp") == RULE_CLI
        assert rule.reason("sales") is None and not rule.excludes("sales")
        assert rule.patterns[-3:] == ("Scratch*", "*_Tmp", "scratch_cli")

    def test_schema_exclusion_splits_configured_from_cli(self, tmp_path):
        env = _project(tmp_path, TWO_SOURCES)
        with cfg.override_settings(project_config_dir=str(env), exclude_schemas="Audit*"):
            rule = schema_exclusion("sales", ["*_Stg"])

        assert rule.configured == ("Audit*", "*_Tmp", "Scratch")
        assert rule.cli == ("*_Stg",)


class TestDatasourcesYaml:
    def test_exclude_schemas_is_a_list_of_trimmed_patterns(self):
        definition = DataSourceDefinition(url_env="DB_URL_MAIN", exclude_schemas=[" *_Stg ", "Replay"])

        assert definition.exclude_schemas == ["*_Stg", "Replay"]

    def test_it_defaults_to_empty(self):
        assert DataSourceDefinition(url_env="DB_URL_MAIN").exclude_schemas == []

    @pytest.mark.parametrize("value, message", [
        ("*_Stg", "must be a list"),
        (["ok", 3], r"exclude_schemas\[1\] must be a string"),
        (["ok", "  "], r"exclude_schemas\[1\] must not be empty"),
        ([""], r"exclude_schemas\[0\] must not be empty"),
        ({"a": 1}, "must be a list"),
    ])
    def test_anything_else_is_refused_with_the_position(self, value, message):
        with pytest.raises(ValueError, match=message):
            DataSourceDefinition(url_env="DB_URL_MAIN", exclude_schemas=value)

    def test_the_error_names_the_file_and_the_key(self):
        with pytest.raises(ValueError, match=r"\[datasources.yaml\] validation error at 'datasources -> a -> exclude_schemas'"):
            validate_datasources_yaml_text("datasources:\n  a:\n    url_env: X\n    exclude_schemas: 5\n")

    def test_accessor_lists_every_source_default_first(self, tmp_path):
        env = _project(tmp_path, TWO_SOURCES)
        with cfg.override_settings(project_config_dir=str(env)):
            assert datasource_exclude_schemas() == {"sales": ("*_Tmp", "Scratch"), "reports": ()}

    def test_accessor_without_the_file(self, tmp_path):
        with cfg.override_settings(project_config_dir=str(tmp_path)):
            assert datasource_exclude_schemas() == {"default": ()}

    def test_the_commented_example_in_the_template_is_valid_when_uncommented(self):
        template = Path(__file__).resolve().parent.parent / "project_config.example" / "datasources.example.yaml"
        text = template.read_text(encoding="utf-8")
        line = next(ln for ln in text.splitlines() if ln.lstrip().startswith("# exclude_schemas:") and "[" in ln)
        uncommented = text.replace(line, line.replace("# exclude_schemas:", "exclude_schemas:", 1).split("  #")[0])

        parsed = validate_datasources_yaml_text(uncommented)

        assert parsed.datasources["sales"].exclude_schemas == ["*_Stg", "Replay"]


class TestSetting:
    def test_exclude_schemas_comes_from_the_environment(self, monkeypatch):
        monkeypatch.delenv("EXCLUDE_SCHEMAS", raising=False)
        assert cfg.Settings().exclude_schemas == ""
        monkeypatch.setenv("EXCLUDE_SCHEMAS", "*_Stg,Replay")
        assert cfg.Settings().exclude_schemas == "*_Stg,Replay"

    def test_env_example_documents_it_empty(self):
        from dotenv import dotenv_values

        values = dotenv_values(Path(__file__).resolve().parent.parent / ".env.example")
        assert values["EXCLUDE_SCHEMAS"] in (None, "")
