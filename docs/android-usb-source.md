# USB observation lifecycle helper

The order_events_usb.UsbOrderConnector helper connects the existing UsbOrderSource
protocol to the existing OrderEvents journal. It opens only a loopback TCP socket
supplied by a trusted host capability. It does not execute ADB, obtain a
reservation, start a native client, classify notification text or choose an action.

The host owns the existing service lifecycle and these exact interfaces:

    SourceAccess(principal, serial)
    UsbForward(serial, port, close_owned, receipt)
    UsbOrderConnector(events, owner=owner, device_id=device_id,
                      source_id=source_id, auth_epoch=auth_epoch,
                      admit=admit, bind_usb=bind_usb)
    connector.start()
    await connector.close()
    connector.status()

The synchronous admit() must freshly authorize the original live run or retained
handle, including the exact generation, current principal/device rights, source
configuration/epoch, configured physical serial and original conversation context.
Removing or replacing that generation must refuse even when the owner and serial
strings still match. Stored task words, a journal row and a principal name cannot
recreate this authority after restart.

The asynchronous bind_usb(access) must return a proved-owned, serial-pinned
forward to the existing companion's local socket, with a private original
acquisition receipt. The host adapter owns durable acquisition intent and
reconciliation across restart. It must never adopt an unknown forward, remove
borrowed forwards, restart ADB or use remove-all. The close_owned() callback returns
exactly True only after that specific owned forward is proved closed. Its
asynchronous form is supported. Receipt and access objects are host-private and
omitted from helper status.

No actual trusted forward adapter or app/Agents lifecycle wiring is supplied by
this module. A owns those existing files and integration. Constructing the helper,
an installed APK, or a synthetic test does not establish installed runtime readiness.

Admission runs before acquisition and after every bind/socket/read await. Blocked
reads retain one pending read while checking authority at bounded intervals,
preserving partial frames. The watch includes only freshly authorized, active
obligations for the configured owner/device/source/epoch; notice-only records and
absent packages do not expand it. More than 32 distinct packages produces
watch_package_limit, preserving every obligation. Package changes reconnect
through the same capability. No provider order number is invented.

A process-wide serial gate excludes another owner/connector on that physical
serial. The existing single host service remains the process authority; this
in-memory gate cannot prove ownership after host restart. Uncertain acquisition
or cleanup retains its serial hold and original capability when available.
Repeated close/start/new connector calls cannot retry that mutation.

Disconnects permit a bounded number of socket reconnects on the same acquired
capability (default three); acquisition is attempted once. Exhaustion is visible.
The durable source epoch/sequence supports replay through original event IDs.
A heartbeat's current source head does not advance the observed cursor. Multiple
OS updates with one hashed notification key remain distinct observations when
their sequence differs, coalescing into the existing single pending wake.
Observation gaps remain visible. No source-head freeze or immediate heartbeat
is required: the unchanged companion waits up to 30 seconds while quiet.

Host admission ends on authority, generation, package or lease loss. Device
collection persists until a watch change: socket closure alone cannot clear it.
During shutdown the helper may send one emptywatch through the still-owned,
still-authorized capability. Only a valid hello confirms that exchange.
All cleanup has one total deadline (default 40 seconds, maximum 60); part is
reserved for exact owned-forward closure. Revoked authority, missing hello or
timeout leaves cleanup_unconfirmed. Forward cleanup uncertainty remains a
separate hold; there is no automatic acquisition/resend to make cleanup green.
A/root owns unresolved device cleanup and real lifecycle authority.

The journal preserves chosen next checks, pending/unknown wakes and responsibility.
The existing Supervision.tick() consumes these records using its original retained
lease/device gate. A source observation never fabricates native acceptance: absent
a live handle, the real supervision path records lease_unavailable, retains the
wake and leaves native receipt absent. Timers provide the existing no-push
fallback; the connector starts no model loop.

Tests use real loopback sockets and the real source, journal, control and
supervision helpers, with a synthetic forward capability. They cover delayed
hello/heartbeat, sequence replay, same-key updates, revocation races, package
scope, serial exclusion, bounded reconnect/cleanup and unknown delivery.
Actual APK/USB/native/routine integration and live acceptance remain root-owned.
