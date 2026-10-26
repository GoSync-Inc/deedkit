-- ANYM-529: search reaches shared contact cards through the few metadata rows that carry one.
CREATE INDEX message_metadata_contact ON message_metadata(message) WHERE metadata ? 'contact';
