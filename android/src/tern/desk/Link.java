package tern.desk;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.URI;
import java.net.URISyntaxException;
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

    /** The certificate at an address. The handshake proves whoever answers has its key; nothing is sent after it. */
    static Answer ask(String base, int ms) {
        URI u = URI.create(base);
        String host = u.getHost();
        Socket raw = new Socket();
        try {
            SSLContext c = SSLContext.getInstance("TLS");
            c.init(null, new TrustManager[] { new Any() }, new SecureRandom());
            raw.connect(new InetSocketAddress(host.startsWith("[") ? host.substring(1, host.length() - 1) : host, u.getPort()), ms);
            raw.setSoTimeout(ms);
            try (SSLSocket s = (SSLSocket) c.getSocketFactory().createSocket(raw, host, u.getPort(), true)) {
                s.startHandshake();
                return new Answer(base, pin(s.getSession().getPeerCertificates()[0].getEncoded()), null);
            }
        } catch (IOException | GeneralSecurityException | RuntimeException e) {
            try { raw.close(); } catch (IOException x) { /* gone already */ }
            return new Answer(base, null, e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage());
        }
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
