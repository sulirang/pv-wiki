# Wiki.js 2.5 GraphQL notes

- Endpoint: `POST {base_url}/graphql` with `Authorization: Bearer {token}`.
- Identity: the pair `locale + path`; page numeric IDs are discovered, never
  assumed.
- Read: `pages.singleByPath(path, locale)` and retain every field needed for a
  full update, especially `content`, `description`, `isPublished`, `isPrivate`,
  `title`, `tags`, `publishStartDate`, `publishEndDate`, `scriptCss`, `scriptJs`,
  and `updatedAt`.
- Create: send content, description, editor (`markdown`), publication flags,
  locale, path, tags, and title.
- Update: check conflicts immediately before the mutation and send complete
  page fields. A detected conflict is terminal for the current lease. Wiki.js
  2.5 exposes no atomic compare-and-swap mutation, so this is best-effort; keep
  one worker and the check-to-update window short.
- Ownership: preserve publish windows, page scripts/styles, and non-Hermes
  tags. Replace only the known managed tag names/prefixes and auto block.
- Validation: an HTTP 2xx response is insufficient. Reject top-level GraphQL
  `errors` and `responseResult.succeeded=false`.
- Race: if another worker creates the page after lookup, refetch once and use
  the update path. Run only one scheduled worker by default.

The adapter owns only the marked auto-generated block. It must not normalize or
rewrite human content outside that block.
