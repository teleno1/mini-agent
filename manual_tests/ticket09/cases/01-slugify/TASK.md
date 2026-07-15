# Slug normalization

`slugify()` is used to generate URL path components, but it mishandles repeated
whitespace, punctuation, and leading or trailing separators.

Update the production implementation so all existing tests pass. Do not modify
the tests. Keep the implementation dependency-free and avoid special-casing
the individual examples.

Verification:

```console
python -m unittest discover -s tests -v
```
