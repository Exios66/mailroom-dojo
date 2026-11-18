"""Element scorers that ship with the Dojo but belong to one dataset; a profile enables them by name."""

from types import MappingProxyType

from .cms_identifiers import CMS_DOC_REF_TYPE, score_cms_doc_ref

__all__ = ["BUNDLED_PLUGINS", "score_cms_doc_ref"]

#: Plug-in field types a profile may name in ``scorer_plugins`` without supplying code.
BUNDLED_PLUGINS = MappingProxyType({CMS_DOC_REF_TYPE: score_cms_doc_ref})
