class CalendarError(Exception):
    """Public fixed error code; never include provider payloads or credentials.

    `field_errors` names the input fields at fault, as [{"field": path, "reason": fixed word}].
    `details` carries fixed, owner-facing text for local CLI errors; the API relays it only for
    the codes in api.PUBLIC_DETAILS (nothing_to_project: rule IDs, duties, reasons, activity labels).
    """

    def __init__(self, code: str, field_errors: list | None = None, details: dict | None = None):
        self.code = code
        self.field_errors = field_errors or []
        self.details = details or {}
        super().__init__(code)
