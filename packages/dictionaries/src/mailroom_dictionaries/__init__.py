"""Versioned controlled vocabularies, taxonomies and the glossary.

Imports only :mod:`mailroom_common` from this workspace, so every other package can read its
vocabularies from here.
"""

from mailroom_dictionaries.dictionary import (
    DATA_DIR,
    Dictionary,
    DictionarySet,
    Entry,
    VocabularyError,
    fold,
    list_dictionaries,
    load_dictionary,
    load_dictionary_dir,
    load_dictionary_file,
    shipped_dictionaries,
)
from mailroom_dictionaries.field_types import FieldType, parse_field_type
from mailroom_dictionaries.glossary import Glossary, Term, load_glossary, render_glossary

__version__ = "0.1.0"

__all__ = [
    "DATA_DIR",
    "Dictionary",
    "DictionarySet",
    "Entry",
    "FieldType",
    "Glossary",
    "Term",
    "VocabularyError",
    "fold",
    "list_dictionaries",
    "load_dictionary",
    "load_dictionary_dir",
    "load_dictionary_file",
    "load_glossary",
    "parse_field_type",
    "render_glossary",
    "shipped_dictionaries",
]
