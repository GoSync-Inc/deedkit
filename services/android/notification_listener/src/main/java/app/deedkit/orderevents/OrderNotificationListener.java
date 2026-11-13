package app.deedkit.orderevents;

import android.net.LocalServerSocket;
import android.net.LocalSocket;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;
import org.json.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;

/** USB source only. No internet, UI actions, text export, approvals or order decisions. */
public final class OrderNotificationListener extends NotificationListenerService {
    private EventJournal journal;
    private LocalServerSocket server;
    private volatile LocalSocket client;
    private volatile long generation;
    private volatile Set<String> packages = Collections.emptySet();
    private volatile boolean stopped;

    @Override public void onListenerConnected() {
        if (server != null) return;
        try {
            journal = new EventJournal(new File(getFilesDir(), "observations"), 256);
            packages = Collections.unmodifiableSet(new HashSet<>(getSharedPreferences("source", MODE_PRIVATE)
                .getStringSet("packages", Collections.emptySet())));
            server = new LocalServerSocket("deedkit-order-events");
            stopped = false;
            LocalServerSocket bound = server;
            long current = ++generation;
            new Thread(() -> serve(bound, current), "order-observation-usb").start();
        } catch (IOException error) { shutdown(); }
    }

    @Override public void onNotificationPosted(StatusBarNotification notification) {
        if (journal == null || !packages.contains(notification.getPackageName())) return;
        try {
            // A key is an opaque hint, never an order ID. No title/text/secret is read or exported.
            JSONObject body = new JSONObject().put("package", notification.getPackageName())
                .put("observation_ref", hash(notification.getKey()))
                .put("observed_at", System.currentTimeMillis() / 1000.0)
                .put("posted_at", notification.getPostTime() / 1000.0);
            journal.append(body.toString());
        } catch (IOException | JSONException error) {
            // No raw exception or notification logging; a broken durable source closes visibly.
            shutdown();
        }
    }

    private static String hash(String value) {
        try {
            byte[] bytes = MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8));
            StringBuilder result = new StringBuilder();
            for (byte b : bytes) result.append(String.format(Locale.ROOT, "%02x", b & 255));
            return result.toString();
        } catch (java.security.NoSuchAlgorithmException error) { throw new IllegalStateException("hash_unavailable"); }
    }

    private void serve(LocalServerSocket bound, long current) {
        while (!stopped && current == generation) {
            try {
                LocalSocket socket = bound.accept();
                if (!EventJournal.usbPeerAllowed(socket.getPeerCredentials().getUid())) {
                    socket.close();
                    continue;
                }
                client = socket;
                stream(socket, current);
            } catch (IOException | RuntimeException error) { if (!stopped && current == generation) shutdown(); }
        }
    }

    private void stream(LocalSocket socket, long current) {
        try (socket) {
            socket.setSoTimeout(5000);
            InputStream input = socket.getInputStream();
            ByteArrayOutputStream request = new ByteArrayOutputStream();
            int value;
            while ((value = input.read()) != '\n') {
                if (value < 0 || request.size() >= 4096) throw new IOException("invalid_request");
                request.write(value);
            }
            JSONObject config = new JSONObject(request.toString(StandardCharsets.UTF_8.name()));
            if (config.getInt("version") != 1) throw new IOException("unsupported_version");
            JSONArray apps = config.getJSONArray("packages");
            if (apps.length() > 32) throw new IOException("too_many_packages");
            Set<String> next = new HashSet<>();
            for (int i = 0; i < apps.length(); i++) {
                String name = apps.getString(i);
                if (!name.matches("[A-Za-z][A-Za-z0-9_]*(\\.[A-Za-z0-9_]+)+") || name.length() > 200)
                    throw new IOException("invalid_package");
                next.add(name);
            }
            if (!getSharedPreferences("source", MODE_PRIVATE).edit().putStringSet("packages", next).commit())
                throw new IOException("configuration_not_persisted");
            packages = Collections.unmodifiableSet(new HashSet<>(next));
            long after = config.getLong("after");
            if (after < 0) throw new IOException("invalid_cursor");
            boolean reset = !journal.epoch.equals(config.optString("source_epoch", ""));
            if (reset) after = 0;
            OutputStream output = socket.getOutputStream();
            send(output, new JSONObject().put("type", "hello").put("version", 1).put("source_epoch", journal.epoch)
                .put("oldest_sequence", journal.oldest()).put("last_sequence", journal.last())
                .put("gap", reset || after < journal.oldest() - 1));
            while (!stopped && current == generation) {
                for (EventJournal.Entry entry : journal.after(after)) {
                    JSONObject body = new JSONObject(entry.body());
                    // Removed apps cannot leak their retained journal metadata on reconnection.
                    if (packages.contains(body.getString("package")))
                        send(output, body.put("type", "observation").put("source_epoch", journal.epoch)
                            .put("sequence", entry.sequence()));
                    after = entry.sequence();
                }
                journal.awaitAfter(after);
                send(output, new JSONObject().put("type", "heartbeat").put("source_epoch", journal.epoch)
                    .put("last_sequence", journal.last()));
            }
        } catch (IOException | JSONException | InterruptedException | RuntimeException error) {
            // Host sees EOF and retains fallback checks. No exception strings in logs.
        } finally { if (client == socket) client = null; }
    }

    private static void send(OutputStream output, JSONObject body) throws IOException {
        output.write((body.toString() + "\n").getBytes(StandardCharsets.UTF_8));
        output.flush();
    }
    private void shutdown() {
        stopped = true;
        generation++;
        try { if (client != null) client.close(); } catch (IOException ignored) {}
        try { if (server != null) server.close(); } catch (IOException ignored) {}
        server = null;
        if (journal != null) synchronized (journal) { journal.notifyAll(); }
    }
    @Override public void onListenerDisconnected() { shutdown(); }
    @Override public void onDestroy() { shutdown(); super.onDestroy(); }
}
