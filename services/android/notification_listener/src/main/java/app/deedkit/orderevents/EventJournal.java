package app.deedkit.orderevents;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;

/** Bounded metadata journal, committed before a stream subscriber is notified. No notification text. */
public final class EventJournal {
    public record Entry(long sequence, String body) {}
    private final File file;
    private final int capacity;
    private final ArrayDeque<Entry> entries = new ArrayDeque<>();
    public final String epoch;
    private long last;

    public EventJournal(File file, int capacity) throws IOException {
        if (capacity < 1 || capacity > 1024) throw new IOException("invalid_capacity");
        this.file = file;
        this.capacity = capacity;
        String savedEpoch = null;
        if (file.isFile()) {
            if (file.length() > 5 * 1024 * 1024) throw new IOException("journal_too_large");
            try (BufferedReader reader = new BufferedReader(new InputStreamReader(
                    new FileInputStream(file), StandardCharsets.UTF_8))) {
                String header = reader.readLine();
                if (header == null) throw new IOException("invalid_journal");
                String[] parts = header.split("\t", 2);
                try { savedEpoch = UUID.fromString(parts[0]).toString(); last = Long.parseLong(parts[1]); }
                catch (RuntimeException error) { throw new IOException("invalid_journal"); }
                String line;
                long previous = 0;
                while ((line = reader.readLine()) != null) {
                    parts = line.split("\t", 2);
                    long sequence;
                    try { sequence = Long.parseLong(parts[0]); }
                    catch (RuntimeException error) { throw new IOException("invalid_journal"); }
                    if (parts.length != 2 || sequence <= previous || sequence > last || parts[1].length() > 4096)
                        throw new IOException("invalid_journal");
                    entries.addLast(new Entry(sequence, parts[1]));
                    if (entries.size() > capacity) entries.removeFirst();
                    previous = sequence;
                }
            }
        }
        epoch = savedEpoch == null ? UUID.randomUUID().toString() : savedEpoch;
        if (savedEpoch == null) persist();
    }

    public static boolean usbPeerAllowed(int uid) { return uid == 2000 || uid == 0; }

    public synchronized long last() { return last; }
    public synchronized long oldest() { return entries.isEmpty() ? last + 1 : entries.getFirst().sequence(); }
    public synchronized List<Entry> after(long sequence) {
        ArrayList<Entry> result = new ArrayList<>();
        for (Entry entry : entries) if (entry.sequence() > sequence) result.add(entry);
        return result;
    }
    public synchronized void awaitAfter(long sequence) throws InterruptedException {
        if (last <= sequence) wait(30000);
    }
    public synchronized void append(String body) throws IOException {
        if (body.length() > 4096 || body.indexOf('\n') >= 0 || body.indexOf('\r') >= 0)
            throw new IOException("invalid_event");
        ArrayDeque<Entry> before = new ArrayDeque<>(entries);
        long previous = last;
        entries.addLast(new Entry(++last, body));
        if (entries.size() > capacity) entries.removeFirst();
        try { persist(); }
        catch (IOException error) { entries.clear(); entries.addAll(before); last = previous; throw error; }
        notifyAll();
    }
    private void persist() throws IOException {
        File temp = new File(file.getParentFile(), file.getName() + ".tmp");
        try (FileOutputStream stream = new FileOutputStream(temp)) {
            StringBuilder body = new StringBuilder(epoch + "\t" + last + "\n");
            for (Entry entry : entries) body.append(entry.sequence()).append('\t').append(entry.body()).append('\n');
            stream.write(body.toString().getBytes(StandardCharsets.UTF_8));
            stream.getFD().sync();
        }
        Files.move(temp.toPath(), file.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
    }
}
