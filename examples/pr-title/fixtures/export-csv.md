# Export reports as CSV

Support keeps asking for the numbers behind the usage report so they can
paste them into a spreadsheet. Adds a download button and a `?format=csv`
parameter on the existing report endpoint. JSON remains the default, so
existing API callers are unaffected.
