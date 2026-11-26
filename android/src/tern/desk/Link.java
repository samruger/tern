package tern.desk;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.URI;
import java.net.URISyntaxException;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.security.cert.X509Certificate;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;

/** The desk's addresses, and whether what answers at one is the desk: its certificate is the approved one, or it isn't.
    Nothing of Android's is used here, so the tests can run it. */
final class Link {
    static final int PORT = 8787;
    static final int HEAD_START = 1500, WAIT = 6000;        // ms: what the first address gets before another may be taken; what each gets in all

    private Link() {}

    /** What an address gave: the certificate's fingerprint, or why there is none. */
    static final class Answer {
        final String base, pin, error;
        Answer(String base, String pin, String error) { this.base = base; this.pin = pin; this.error = error; }
    }

    /** An address as typed, as https://host:port. Blank is null. */
    static String base(String typed) {
        String t = typed == null ? "" : typed.trim();
        if (t.isEmpty()) return null;
        if (t.toLowerCase(Locale.ROOT).startsWith("http://")) throw new IllegalArgumentException("Only https addresses work: " + t);
        if (!t.contains("://")) t = "https://" + t;
        URI u;
        try { u = new URI(t); } catch (URISyntaxException e) { throw new IllegalArgumentException("Not an address: " + typed.trim()); }
        String path = u.getRawPath();
        if (!"https".equalsIgnoreCase(u.getScheme()) || u.getHost() == null || u.getRawUserInfo() != null || u.getRawQuery() != null
                || u.getRawFragment() != null || !(path == null || path.isEmpty() || path.equals("/")) || u.getPort() == 0 || u.getPort() > 65535)
            throw new IllegalArgumentException("Not an address: " + typed.trim());
        return "https://" + u.getHost().toLowerCase(Locale.ROOT) + ":" + (u.getPort() < 0 ? PORT : u.getPort());
    }

    static String pin(byte[] der) throws GeneralSecurityException {
        StringBuilder s = new StringBuilder();
        for (byte b : MessageDigest.getInstance("SHA-256").digest(der)) s.append(s.length() > 0 ? ":" : "").append(String.format(Locale.ROOT, "%02X", b & 0xff));
        return s.toString();
    }

    /** Whether a page's address is at one of these: the same scheme, host and port. */
    static boolean at(String url, String base) {
        try {
            URI u = new URI(url), b = new URI(base);
            return "https".equalsIgnoreCase(u.getScheme()) && b.getHost().equalsIgnoreCase(u.getHost())
                && (u.getPort() < 0 ? 443 : u.getPort()) == b.getPort();
        } catch (URISyntaxException e) {
            return false;
        }
    }

    /** A TLS connection to an address, its handshake done: any certificate is taken here, for the caller to compare. */
    private static SSLSocket tls(URI u, int ms) throws IOException, GeneralSecurityException {
        String host = u.getHost();
        SSLContext c = SSLContext.getInstance("TLS");
        c.init(null, new TrustManager[] { new Any() }, new SecureRandom());
        Socket raw = new Socket();
        try {
            raw.connect(new InetSocketAddress(host.startsWith("[") ? host.substring(1, host.length() - 1) : host, u.getPort()), ms);
            raw.setSoTimeout(ms);
            SSLSocket s = (SSLSocket) c.getSocketFactory().createSocket(raw, host, u.getPort(), true);
            s.startHandshake();
            return s;
        } catch (IOException | RuntimeException e) {
            try { raw.close(); } catch (IOException x) { /* gone already */ }
            throw e;
        }
    }

    private static String why(Exception e) { return e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage(); }

    /** The certificate at an address. The handshake proves whoever answers has its key; nothing is sent after it. */
    static Answer ask(String base, int ms) {
        try (SSLSocket s = tls(URI.create(base), ms)) {
            return new Answer(base, pin(s.getSession().getPeerCertificates()[0].getEncoded()), null);
        } catch (IOException | GeneralSecurityException | RuntimeException e) {
            return new Answer(base, null, why(e));
        }
    }

    /** What the desk answered a POST: its status, body and Set-Cookie lines. */
    static final class Reply {
        final int code;
        final String body;
        final List<String> cookies;
        Reply(int code, String body, List<String> cookies) { this.code = code; this.body = body; this.cookies = cookies; }
    }

    /** POSTs JSON to the desk, only once the certificate that answered is the approved one: nothing is sent to any other. */
    static Reply post(String base, String path, String json, String pin, int ms) throws IOException {
        if (pin == null) throw new IOException("no certificate is approved");
        URI u = URI.create(base);
        try (SSLSocket s = tls(u, ms)) {
            if (!pin.equals(pin(s.getSession().getPeerCertificates()[0].getEncoded()))) throw new IOException(base.substring(8) + " answered with another certificate");
            byte[] body = json.getBytes(StandardCharsets.UTF_8);
            String head = "POST " + path + " HTTP/1.1\r\nHost: " + u.getHost() + ":" + u.getPort() + "\r\nContent-Type: application/json\r\nContent-Length: " + body.length
                + "\r\nUser-Agent: tern-app\r\nConnection: close\r\n\r\n";
            OutputStream out = s.getOutputStream();
            out.write(head.getBytes(StandardCharsets.US_ASCII));
            out.write(body);
            out.flush();
            return read(s.getInputStream());
        } catch (GeneralSecurityException | RuntimeException e) {
            throw new IOException(why(e), e);
        }
    }

    private static Reply read(InputStream in) throws IOException {
        ByteArrayOutputStream got = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int end = -1, want = -1;
        for (int n; (want < 0 || got.size() < want) && (n = in.read(buf)) > 0; ) {
            got.write(buf, 0, n);
            if (got.size() > 1 << 20) throw new IOException("too big an answer");
            if (end < 0 && (end = find(got.toByteArray())) >= 0) {
                Matcher m = LENGTH.matcher(new String(got.toByteArray(), 0, end, StandardCharsets.ISO_8859_1));
                if (m.find()) want = end + 4 + Integer.parseInt(m.group(1));
            }
        }
        byte[] all = got.toByteArray();
        if (end < 0) end = find(all);
        if (end < 0) throw new IOException("no answer");
        String[] lines = new String(all, 0, end, StandardCharsets.ISO_8859_1).split("\r\n");
        String[] status = lines[0].split(" ", 3);
        if (status.length < 2 || !status[0].startsWith("HTTP/")) throw new IOException("not an HTTP answer");
        List<String> cookies = new ArrayList<>();
        for (String l : lines) if (l.regionMatches(true, 0, "Set-Cookie:", 0, 11)) cookies.add(l.substring(11).trim());
        int from = end + 4, to = want >= 0 ? Math.min(want, all.length) : all.length;
        try {
            return new Reply(Integer.parseInt(status[1]), new String(all, from, Math.max(0, to - from), StandardCharsets.UTF_8), cookies);
        } catch (NumberFormatException e) {
            throw new IOException("not an HTTP answer");
        }
    }

    private static final Pattern LENGTH = Pattern.compile("(?im)^Content-Length:\\s*(\\d{1,9})\\s*$");

    /** Where the head ends: the blank line's start, or -1. */
    private static int find(byte[] b) {
        for (int i = 0; i + 3 < b.length; i++) if (b[i] == '\r' && b[i + 1] == '\n' && b[i + 2] == '\r' && b[i + 3] == '\n') return i;
        return -1;
    }

    private static boolean good(Answer a, String pin) { return a != null && a.pin != null && (pin == null || pin.equals(a.pin)); }

    /** Every address is asked at once. The first with the approved certificate (any, with none approved) goes first, the
        others after its head start. With none, all the answers there were, for saying why. */
    static List<Answer> find(List<String> bases, String pin) {
        ExecutorService pool = Executors.newFixedThreadPool(Math.max(1, bases.size()));
        List<Answer> got = new ArrayList<>();
        try {
            List<Future<Answer>> asked = new ArrayList<>();
            for (String b : bases) asked.add(pool.submit(() -> ask(b, WAIT)));
            long t0 = System.nanoTime();
            for (int i = 0; i < asked.size(); i++) {                                  // in order, the last for as long as it takes
                Answer a = get(asked.get(i), (i < asked.size() - 1 ? HEAD_START : 2 * WAIT) - (System.nanoTime() - t0) / 1_000_000);
                if (good(a, pin)) { got.add(a); return got; }
            }
            for (int i = 0; i < asked.size(); i++) {                                  // one that was passed over for being slow
                Answer a = get(asked.get(i), 2 * WAIT - (System.nanoTime() - t0) / 1_000_000);
                if (good(a, pin)) { got.clear(); got.add(a); return got; }
                got.add(a != null ? a : new Answer(bases.get(i), null, "no answer"));
            }
            return got;
        } finally {
            pool.shutdownNow();
        }
    }

    private static Answer get(Future<Answer> f, long ms) {
        try {
            return f.get(Math.max(1, ms), TimeUnit.MILLISECONDS);
        } catch (TimeoutException e) {
            return null;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return null;
        } catch (java.util.concurrent.ExecutionException e) {
            return null;
        }
    }

    /** Takes any certificate, to read it: what it is compared with is the caller's. */
    private static final class Any implements X509TrustManager {
        @Override public void checkClientTrusted(X509Certificate[] chain, String auth) {}
        @Override public void checkServerTrusted(X509Certificate[] chain, String auth) {}
        @Override public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
    }
}
