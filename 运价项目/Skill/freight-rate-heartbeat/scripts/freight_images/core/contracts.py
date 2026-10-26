"""OCR source inventory: declared image facts, never database conclusions."""
SAILING_MATRIX_TYPE = "sailing_matrix"
MATRIX_TYPES = frozenset({"rate_matrix", SAILING_MATRIX_TYPE})
OMITTED_POL_MARKERS = ("起运港还包含", "只允许显式一组POL")
SUPPORTED_PRICE_COLUMNS = ("20GP", "40HQ/HC", "40NOR")
