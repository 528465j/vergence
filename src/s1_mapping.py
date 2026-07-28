"""Stage 1 — Schema resolution and canonical mapping. Phase 2.

The only module permitted to call a language model.

    Tier 1  registry exact match            deterministic, free, instant
    Tier 2  synonym / fuzzy match           deterministic
    Tier 3  model proposal                  only what tiers 1-2 could not resolve
    Gate    low-confidence or first-time    -> human review queue

    def resolve_columns(headers, sample_rows, client_id, registry, llm=None)
            -> list[MappingProposal]

`llm=None` is the default and must stay that way. With no model attached the
pipeline still runs to a correct result; it simply routes more columns to the
review queue. Approving a proposal writes it to registry/mappings.json, after
which it resolves at Tier 1 forever.
"""

raise NotImplementedError("Phase 2")
