# fix nullpointer

Customers on the Pro plan intermittently got a 500 on the invoice page.
`Invoice.billing_contact` is nullable for accounts created before the
self-serve flow shipped, and the renderer assumed it was always set.

Guards the lookup and falls back to the account owner. Added a regression
test with a fixture invoice that has no billing contact.
