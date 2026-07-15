# Existing Wiki.js setup

PV wiki targets Wiki.js 2.5 GraphQL at `POST $WIKIJS_URL/graphql`. Wiki.js 3.x
is not supported by this release.

Create a dedicated Wiki.js group and API key. Give it only the page read/write
permissions required beneath the configured path (for example `products/*`).
Do not grant user, group, navigation, system, or asset administration. Use a
staging prefix first, such as `pv-wiki-staging`.

Set:

```text
WIKIJS_URL=https://wiki.example.com
WIKIJS_TOKEN=<replace-with-restricted-token>
WIKIJS_LOCALE=en
WIKIJS_PATH_PREFIX=products
WIKIJS_NEW_PAGE_PRIVATE=true
WIKIJS_NEW_PAGE_PUBLISHED=false
```

New pages are private drafts by default. After validating the staging output
and confirming the Wiki.js access boundary, set the two visibility variables
explicitly if new pages should be published. Existing pages always retain the
visibility returned by Wiki.js; the skill never changes it implicitly.

The adapter looks up a page with `pages.singleByPath(path, locale)`, then calls
`pages.create` or `pages.update`. It submits complete page fields on update,
checks the GraphQL error envelope and Wiki.js `responseResult`, and calls
`pages.checkConflicts` using the fetched `updatedAt` before updating.

Page paths use the stable database product ID plus a short hash, without brand
or family hints. This prevents slug collisions and avoids moving a page when
catalogue metadata is corrected.

Wiki.js directories are path-based; this skill does not edit global navigation.
Datasheets are external links in v0.1 and are not uploaded to Wiki.js storage.
