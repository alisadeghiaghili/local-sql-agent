# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``keywords:`` and ``description:`` in ``datasources.yaml``.

The keywords steer which data source a question is routed to
(``retrieval.source_selector``); a typo in them would silently route
questions to the wrong source, so the file is refused at load time for a
keywords value that is not a list of distinct, non-empty strings.
"""

from __future__ import annotations

import pytest
import yaml

from config import Settings
from database.datasources import (
    DataSourceDefinition,
    datasource_descriptions,
    datasource_keywords,
    get_datasource,
    load_datasources_config,
    reset_datasources_cache,
    validate_datasources_yaml_text,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    reset_datasources_cache()
    yield
    reset_datasources_cache()


def _text(**source_fields) -> str:
    sources = {
        "sales": {"url_env": "DB_URL_SALES", **source_fields},
        "inventory": {"url_env": "DB_URL_INVENTORY"},
    }
    return yaml.dump({"default": "sales", "datasources": sources}, sort_keys=False, allow_unicode=True)


def _keywords(value) -> DataSourceDefinition:
    return validate_datasources_yaml_text(_text(keywords=value)).datasources["sales"]


class TestAccepted:
    def test_keywords_are_optional_and_default_to_none(self):
        config = validate_datasources_yaml_text(_text())
        assert config.datasources["sales"].keywords == []

    def test_a_list_of_persian_and_english_phrases_is_kept_in_order(self):
        phrases = ["stock level", "انبار", "موجودی کالا", "SKU"]
        assert _keywords(phrases).keywords == phrases

    def test_phrases_are_stripped(self):
        assert _keywords(["  stock  ", "\tbin\n"]).keywords == ["stock", "bin"]

    def test_an_empty_list_is_the_same_as_no_keywords(self):
        assert _keywords([]).keywords == []

    def test_the_same_phrase_under_two_sources_is_allowed(self):
        # A shared word is a tie at selection time, not a configuration
        # error: only a repeat inside one list is refused.
        text = yaml.dump({
            "default": "a",
            "datasources": {
                "a": {"url_env": "DB_A", "keywords": ["report"]},
                "b": {"url_env": "DB_B", "keywords": ["report"]},
            },
        })
        config = validate_datasources_yaml_text(text)
        assert config.datasources["a"].keywords == config.datasources["b"].keywords

    def test_description_still_works_and_keywords_are_independent_of_it(self):
        config = validate_datasources_yaml_text(_text(description="Sales warehouse", keywords=["revenue"]))
        assert config.datasources["sales"].description == "Sales warehouse"

    def test_keywords_work_in_the_structured_form_too(self):
        text = yaml.dump({"datasources": {"main": {
            "host": "db1", "database": "Sales", "trusted_connection": True, "keywords": ["revenue"],
        }}})
        assert validate_datasources_yaml_text(text).datasources["main"].keywords == ["revenue"]


class TestRefused:
    @pytest.mark.parametrize("value", ["stock", "stock, bin", {"a": 1}, 3, True, None])
    def test_a_value_that_is_not_a_list_is_refused(self, value):
        with pytest.raises(ValueError, match=r"datasources -> sales -> keywords.*must be a list"):
            _keywords(value)

    @pytest.mark.parametrize("item", [1, 2.5, True, None, ["nested"], {"k": "v"}])
    def test_an_item_that_is_not_a_string_is_refused(self, item):
        with pytest.raises(ValueError, match=r"keywords\[1\] must be a string"):
            _keywords(["stock", item])

    @pytest.mark.parametrize("blank", ["", "   ", "\t", "‌", "‌ ‌"])
    def test_an_empty_or_blank_string_is_refused(self, blank):
        with pytest.raises(ValueError, match=r"keywords\[0\] must not be empty"):
            _keywords([blank, "stock"])

    def test_a_repeated_phrase_is_refused(self):
        with pytest.raises(ValueError, match=r"keywords lists 'stock' twice"):
            _keywords(["stock", "bin", "stock"])

    @pytest.mark.parametrize("pair", [
        ("stock", "STOCK"),
        ("stock level", "stock   level"),
        ("كارگزار", "کارگزار"),            # Arabic kaf / Persian kaf
        ("وضعيت", "وضعیت"),                # Arabic yeh / Persian yeh
        ("می‌خواهم", "میخواهم"),   # ZWNJ
        ("1402", "۱۴۰۲"),                  # digits
    ])
    def test_two_spellings_that_fold_to_the_same_text_are_a_repeat(self, pair):
        with pytest.raises(ValueError, match=r"folds to the same text as"):
            _keywords(list(pair))

    def test_the_message_names_the_source_and_the_field(self):
        with pytest.raises(ValueError) as caught:
            _keywords(["a", "a"])
        message = str(caught.value)
        assert "[datasources.yaml]" in message
        assert "datasources -> sales -> keywords" in message

    def test_unknown_keys_are_still_refused(self):
        with pytest.raises(ValueError, match="keyword"):
            validate_datasources_yaml_text(_text(keyword=["stock"]))

    def test_the_model_refuses_directly_too(self):
        with pytest.raises(ValueError):
            DataSourceDefinition(url_env="DB_URL_MAIN", keywords=["a", "A"])


class TestAccessors:
    def _settings(self, tmp_path, **source_fields) -> Settings:
        (tmp_path / "datasources.yaml").write_text(_text(**source_fields), encoding="utf-8")
        return Settings(project_config_dir=str(tmp_path))

    def test_keywords_and_descriptions_are_listed_default_first(self, tmp_path):
        settings = self._settings(tmp_path, description="Sales warehouse", keywords=["revenue", "فروش"])
        assert datasource_keywords(settings) == {"sales": ("revenue", "فروش"), "inventory": ()}
        assert datasource_descriptions(settings) == {"sales": "Sales warehouse", "inventory": ""}

    def test_without_a_file_there_is_one_source_with_nothing(self, tmp_path):
        settings = Settings(project_config_dir=str(tmp_path))
        assert datasource_keywords(settings) == {"default": ()}
        assert datasource_descriptions(settings) == {"default": ""}

    def test_a_resolved_source_carries_its_keywords(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DB_URL_SALES", "mssql+pyodbc://u@h/db?driver=ODBC+Driver+17+for+SQL+Server")
        settings = self._settings(tmp_path, keywords=["revenue"])
        assert get_datasource("sales", settings).keywords == ("revenue",)
        assert get_datasource("inventory", settings).keywords == ()

    def test_an_invalid_file_on_disk_is_refused_when_loaded(self, tmp_path):
        settings = self._settings(tmp_path, keywords=["a", "a"])
        with pytest.raises(ValueError, match="twice"):
            load_datasources_config(settings)

    def test_the_example_file_documents_the_keys_and_validates(self):
        from pathlib import Path

        example = Path(__file__).resolve().parent.parent / "project_config.example" / "datasources.example.yaml"
        text = example.read_text(encoding="utf-8")
        assert "keywords" in text and "description" in text
        config = validate_datasources_yaml_text(text)
        assert config.datasources["sales"].keywords
        assert config.datasources["inventory"].keywords
