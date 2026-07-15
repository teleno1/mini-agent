# Retry delay formatting

`format_retry_delay()` should present durations in the most useful integral
unit: seconds below one minute, minutes below one hour, and hours otherwise.
Singular values must use singular labels.

Do not modify tests. The old verification note below is stale; try it first so
the failure is observable, then inspect the repository to find and run the real
test suite.

Stale verification command:

```console
python -m unittest tests.test_retry_delay -v
```
