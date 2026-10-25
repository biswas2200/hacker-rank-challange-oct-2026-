You are reading one screenshot of an internal admin tool at an online shop.
Write down every data record the screen shows. Do not judge or summarize.

A record is anything with an id like account_123, search_45, support_9538,
analytics_7, export_12, data_flow_3, backup_88, audit_record_5, tombstone_9,
legal_hold_2, marketing_1 or object_19. Records can appear in tables, side
panels, headers ("user_11 · account_5762") or activity lists.

For each record give:
- resource_id: exactly as shown.
- status: the record's status word as shown (for example Active, Deleted,
  Found, Not found, Identifiable, Anonymized, Redacted, Enabled, Disabled,
  Blocked, Restorable), or null.
- data_category: the kind of record if the screen says it, or null.
- user_id: a user_ reference shown for this record, or null.
- name, email, phone, email_hash: shown for this record, or null.
  Use null when a value is masked, hidden or redacted.
- enabled: true or false for connectors and data flows, or null.
- restore_allowed: true or false for backups, or null.
- parent_resource_id: another record this one belongs to, or null.

Also give screen_subject_user_id: the user_ id the whole screen is about
(for example a "Subject filter: user_03" or a customer header), or null.

Only copy what is visible. Never guess a value.

Files and documents listed by name (for example delivery_confirmation_19.pdf)
are records too: use the exact file name as resource_id.
