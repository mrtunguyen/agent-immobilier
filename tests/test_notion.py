"""Notion sync against a fake client: data-source resolution and property names.

Notion API 2025-09-03 moved properties off the database and onto a data source,
and a column named "Profile " (trailing space) is indistinguishable from
"Profile" in the UI. Both cost real listings when they go wrong silently, so
both are pinned here.
"""

from __future__ import annotations

import pytest

from scout.dedupe import Store
from scout.parsers.base import Listing
from scout.sync_notion import NotionSync

DATA_SOURCE_ID = "ds-1"

FULL_SCHEMA = {
    "Name": {"type": "title"},
    "Listing Key": {"type": "rich_text"},
    "Profile": {"type": "rich_text"},
    "Score": {"type": "number"},
    "Price": {"type": "number"},
    "Surface": {"type": "number"},
    "Yield %": {"type": "number"},
    "City": {"type": "rich_text"},
    "URL": {"type": "url"},
    "Status": {"type": "select"},
    "Analysis": {"type": "rich_text"},
}


class FakeNotion:
    """Stands in for notion_client.Client, recording what it was asked to do."""

    def __init__(self, schema=None, existing_page=None, data_sources=None):
        self.schema = FULL_SCHEMA if schema is None else schema
        self.existing_page = existing_page
        self._data_sources = (
            [{"id": DATA_SOURCE_ID, "name": "Rental scout"}]
            if data_sources is None
            else data_sources
        )
        self.created: list[dict] = []
        self.updated: list[dict] = []
        self.queries: list[dict] = []

        outer = self

        class Databases:
            def retrieve(self, database_id):
                return {"object": "database", "data_sources": outer._data_sources}

        class DataSources:
            def retrieve(self, data_source_id):
                return {"id": data_source_id, "properties": outer.schema}

            def query(self, data_source_id, filter, page_size=1):
                outer.queries.append(filter)
                results = [{"id": outer.existing_page}] if outer.existing_page else []
                return {"results": results}

        class Pages:
            def create(self, parent, properties):
                outer.created.append({"parent": parent, "properties": properties})
                return {"id": "new-page"}

            def update(self, page_id, properties):
                outer.updated.append({"page_id": page_id, "properties": properties})
                return {"id": page_id}

        self.databases, self.data_sources, self.pages = Databases(), DataSources(), Pages()


@pytest.fixture
def sync(monkeypatch):
    """A NotionSync wired to a fake client, without importing notion_client."""

    def build(**kwargs):
        fake = FakeNotion(**kwargs)
        obj = NotionSync.__new__(NotionSync)
        obj.client = fake
        obj.database_id = "db-1"
        obj._data_source_id = None
        obj._names = None
        return obj, fake

    return build


@pytest.fixture
def row(tmp_path):
    with Store(tmp_path / "db.sqlite3") as store:
        key = store.insert(
            Listing(
                site="seloger",
                external_id="1",
                url="https://www.seloger.com/annonces/1.htm",
                title="Duplex à vendre",
                price_eur=160_000,
                surface_m2=48.9,
                rooms=2,
                city="Montreuil",
                postal_code="93100",
            ),
            ["default"],
        )
        store.save_analysis(
            key,
            {"score": 75, "gross_yield_pct": 5.69, "analysis": "46% below DVF."},
            "default",
        )
        yield store.get(key, "default")


VERDICT = {"score": 75, "gross_yield_pct": 5.69, "analysis": "46% below DVF."}


def test_creates_a_page_parented_to_the_data_source(sync, row):
    obj, fake = sync()
    page_id = obj.upsert(row, VERDICT, profile="default")

    assert page_id == "new-page"
    # The 2025-09-03 API parents pages on a data source, not the database.
    assert fake.created[0]["parent"] == {
        "type": "data_source_id",
        "data_source_id": DATA_SOURCE_ID,
    }
    props = fake.created[0]["properties"]
    assert props["Score"] == {"number": 75}
    assert props["Price"] == {"number": 160_000}
    assert props["Status"] == {"select": {"name": "New"}}
    assert props["Profile"]["rich_text"][0]["text"]["content"] == "default"


def test_a_trailing_space_in_a_column_name_still_matches(sync, row):
    """A column called "Profile " looks identical to "Profile" in Notion."""
    schema = dict(FULL_SCHEMA)
    schema["Profile "] = schema.pop("Profile")
    obj, fake = sync(schema=schema)

    assert obj.upsert(row, VERDICT, profile="studio-cashflow") == "new-page"
    props = fake.created[0]["properties"]
    # Written under the name Notion actually uses, not the one we expected.
    assert "Profile " in props
    assert "Profile" not in props
    assert props["Profile "]["rich_text"][0]["text"]["content"] == "studio-cashflow"


def test_case_and_spacing_differences_are_tolerated(sync, row):
    schema = {"name": {"type": "title"}, " LISTING KEY ": {"type": "rich_text"}}
    obj, fake = sync(schema=schema)

    assert obj.upsert(row, VERDICT, profile="default") == "new-page"
    assert set(fake.created[0]["properties"]) == {"name", " LISTING KEY "}


def test_missing_optional_columns_are_skipped_not_fatal(sync, row):
    """A half-built database should still receive what it can hold."""
    obj, fake = sync(schema={"Name": {"type": "title"}, "Listing Key": {"type": "rich_text"}})

    assert obj.upsert(row, VERDICT, profile="default") == "new-page"
    assert set(fake.created[0]["properties"]) == {"Name", "Listing Key"}


def test_missing_required_column_refuses_to_write(sync, row, caplog):
    obj, fake = sync(schema={"Score": {"type": "number"}})

    with caplog.at_level("WARNING"):
        assert obj.upsert(row, VERDICT, profile="default") is None
    assert fake.created == []
    assert "missing required" in caplog.text


def test_a_database_with_no_data_source_is_reported(sync, row, caplog):
    obj, fake = sync(data_sources=[])

    with caplog.at_level("WARNING"):
        assert obj.upsert(row, VERDICT, profile="default") is None
    assert "no data source" in caplog.text


def test_an_existing_row_is_updated_and_status_left_alone(sync, row):
    """Status belongs to the user once the row exists."""
    obj, fake = sync(existing_page="page-42")
    page_id = obj.upsert(row, VERDICT, profile="default")

    assert page_id == "page-42"
    assert fake.created == []
    assert "Status" not in fake.updated[0]["properties"]


def test_lookup_matches_on_key_and_profile(sync, row):
    obj, fake = sync(existing_page="page-42")
    obj.upsert(row, VERDICT, profile="family-t3")

    conditions = fake.queries[0]["and"]
    assert {"property": "Listing Key", "rich_text": {"equals": row["key"]}} in conditions
    assert {"property": "Profile", "rich_text": {"equals": "family-t3"}} in conditions


def test_lookup_falls_back_to_key_only_without_a_profile_column(sync, row):
    schema = {k: v for k, v in FULL_SCHEMA.items() if k != "Profile"}
    obj, fake = sync(schema=schema, existing_page="page-42")
    obj.upsert(row, VERDICT, profile="default")

    assert fake.queries[0] == {
        "property": "Listing Key",
        "rich_text": {"equals": row["key"]},
    }


def test_schema_is_fetched_once_across_many_upserts(sync, row):
    obj, fake = sync()
    calls = []
    original = fake.data_sources.retrieve
    fake.data_sources.retrieve = lambda **kw: (calls.append(kw) or original(**kw))

    for _ in range(3):
        obj.upsert(row, VERDICT, profile="default")

    assert len(calls) == 1
    assert len(fake.created) == 3
