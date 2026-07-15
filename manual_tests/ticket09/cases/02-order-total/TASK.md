# Order totals

Customers with at least 100 points receive the discount associated with their
tier. The current order total calculation produces incorrect totals and can
return negative amounts when a discount exceeds the subtotal.

Inspect the project and update production code so the tests pass. Do not modify
tests. Preserve integer-cent arithmetic and keep tier rules in their existing
module rather than duplicating them in the order calculation.

Verification:

```console
python -m unittest discover -s tests -v
```
