package tern.desk;

import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/** What the app signs and sends, as the desk reads it (tern/devices.py): the texts, the count, and reading the answers.
    Nothing of Android's is used here, so the tests can run it. */
final class Act {
    static final String SESSION = "{\"op\":\"session\"}";      // the sign-in's command, exactly
    static final String COOKIE = "__Host-tern_a";               // the app's session cookie
    static final int POLL = 2000, PAIR_WAIT = 5 * 60 * 1000;    // ms: how often the pairing is asked about; for how long

    private Act() {}

    /** Where the next count is kept. keep() must have it stored when it returns true. */
    interface Count {
        long kept();
        boolean keep(long next);
    }

    /** The next count: from 1, always up, and kept before it is used, so that a crash never sends one twice. */
    static synchronized long next(Count c) {
        long n = Math.max(1, c.kept());
        if (!c.keep(n + 1)) throw new IllegalStateException("the app could not keep its count");
        return n;
    }

    /** The signed text: {"d","n","ts","cmd"} in that order, compact. The command is one JSON object, as the page made it. */
    static String payload(String device, long n, double ts, String cmdJson) {
        if (!command(cmdJson) || !Double.isFinite(ts)) throw new IllegalArgumentException("not a command");
        return "{\"d\":" + quote(device) + ",\"n\":" + n + ",\"ts\":" + String.format(Locale.ROOT, "%.3f", ts) + ",\"cmd\":" + cmdJson.trim() + "}";
    }

    /** Whether the text is one JSON object, all of it: what the page may ask to have signed. */
    static boolean command(String cmdJson) {
        try {
            return parse(cmdJson) instanceof Map;
        } catch (IllegalArgumentException e) {
            return false;
        }
    }

    /** What /app/session and /act take: the payload as signed, its signature, and which key signed it. */
    static String body(String payload, String sigB64, boolean live) {
        return "{\"payload\":" + quote(payload) + ",\"sig\":" + quote(sigB64) + ",\"key\":" + quote(live ? "live" : "everyday") + "}";
    }

    /** What the everyday key signs to ask for pairing. */
    static String pairText(String code, String name, String everydayB64, String liveB64) {
        return "pair\n" + code + "\n" + name + "\n" + everydayB64 + "\n" + liveB64;
    }

    static String pairBody(String code, String name, String everydayB64, String liveB64, String sigB64) {
        return "{\"code\":" + quote(code) + ",\"name\":" + quote(name) + ",\"everyday\":" + quote(everydayB64) + ",\"live\":" + quote(liveB64)
            + ",\"sig\":" + quote(sigB64) + "}";
    }

    static String offerBody(String offer) { return "{\"offer\":" + quote(offer) + "}"; }

    static String b64(byte[] b) { return Base64.getEncoder().encodeToString(b); }

    /** Both public keys' words, as the desk shows them: "ABCD EF01 2345". */
    static String words(String everydayB64, String liveB64) {
        try {
            MessageDigest h = MessageDigest.getInstance("SHA-256");
            h.update(Base64.getDecoder().decode(everydayB64));
            h.update(Base64.getDecoder().decode(liveB64));
            StringBuilder s = new StringBuilder();
            byte[] d = h.digest();
            for (int i = 0; i < 6; i++) s.append(i == 2 || i == 4 ? " " : "").append(String.format(Locale.ROOT, "%02X", d[i] & 0xff));
            return s.toString();
        } catch (GeneralSecurityException e) {
            throw new IllegalStateException(e);
        }
    }

    /** A JSON string, all ASCII: safe as JSON and as a JavaScript literal (no raw line ends, quotes or U+2028). */
    static String quote(String t) {
        StringBuilder s = new StringBuilder(t.length() + 2).append('"');
        for (int i = 0; i < t.length(); i++) {
            char c = t.charAt(i);
            if (c == '"' || c == '\\') s.append('\\').append(c);
            else if (c < 0x20 || c > 0x7e || c == '<' || c == '>' || c == '&' || c == '\'') s.append(String.format(Locale.ROOT, "\\u%04x", (int) c));
            else s.append(c);
        }
        return s.append('"').toString();
    }

    // ---------------------------------------------------------------- reading JSON: the desk's answers, and the page's commands

    /** Null as JSON has it, apart from a missing key. */
    static final Object NULL = new Object();

    /** One JSON value, all of the text: a Map, List, String, Double, Boolean or NULL. Anything else throws IllegalArgumentException. */
    static Object parse(String text) {
        if (text == null) throw new IllegalArgumentException("no JSON");
        Reader r = new Reader(text);
        Object v = r.value(0);
        r.space();
        if (r.i != text.length()) throw new IllegalArgumentException("more after the JSON");
        return v;
    }

    /** The text as a JSON object, or an empty one if it isn't one. */
    @SuppressWarnings("unchecked")
    static Map<String, Object> object(String text) {
        try {
            Object v = parse(text);
            if (v instanceof Map) return (Map<String, Object>) v;
        } catch (IllegalArgumentException e) { /* not JSON */ }
        return new LinkedHashMap<>();
    }

    /** A string at the top of a JSON object, or null. */
    static String field(String text, String key) {
        Object v = object(text).get(key);
        return v instanceof String ? (String) v : null;
    }

    /** What a command does, in a few plain words, for the fingerprint prompt: "live on, trading base". */
    static String said(String cmdJson) {
        Map<String, Object> c = object(cmdJson);
        String op = c.get("op") instanceof String ? (String) c.get("op") : "a command";
        String v = c.get("variant") instanceof String ? (String) c.get("variant") : null;
        boolean on = Boolean.TRUE.equals(c.get("on"));
        String t;
        switch (op) {
            case "live": t = on ? "live on" + (v == null ? "" : ", trading " + v) : "live off"; break;
            case "variant_on": t = (on ? "turn on " : "turn off ") + (v == null ? "a variant" : v); break;
            case "set": t = "set " + shown(c.get("key")) + " to " + shown(c.get("value")); break;
            default: t = op.replace('_', ' ') + (v == null ? "" : " " + v);
        }
        return t.length() > 120 ? t.substring(0, 119) + "\u2026" : t;
    }

    private static String shown(Object v) {
        if (v instanceof Double) {
            double d = (Double) v;
            return d == Math.rint(d) && Math.abs(d) < 1e15 ? Long.toString((long) d) : v.toString();
        }
        if (v == NULL || v == null) return "off";
        return v instanceof String || v instanceof Boolean ? v.toString() : "a list";
    }

    /** What a refusal says, as the desk wrote it. */
    static String error(Link.Reply r) {
        String e = field(r.body, "error");
        return e != null ? e : r.code == 403 && !r.body.isEmpty() && !r.body.startsWith("{") ? r.body.trim() : "tern web answered " + r.code;
    }

    /** The app's session cookie, name=value, among an answer's Set-Cookie lines. */
    static String appCookie(List<String> setCookies) {
        for (String c : setCookies) {
            String nv = c.split(";", 2)[0].trim();
            if (nv.startsWith(COOKIE + "=") && nv.length() > COOKIE.length() + 1) return nv;
        }
        return null;
    }

    private static final class Reader {
        final String t;
        int i;
        Reader(String t) { this.t = t; }

        void space() { while (i < t.length() && " \t\n\r".indexOf(t.charAt(i)) >= 0) i++; }

        IllegalArgumentException bad() { return new IllegalArgumentException("not JSON at " + i); }

        char peek() { if (i >= t.length()) throw bad(); return t.charAt(i); }

        void expect(String word) { if (!t.startsWith(word, i)) throw bad(); i += word.length(); }

        Object value(int depth) {
            if (depth > 64) throw bad();
            space();
            char c = peek();
            switch (c) {
                case '{': {
                    i++;
                    Map<String, Object> m = new LinkedHashMap<>();
                    space();
                    if (peek() == '}') { i++; return m; }
                    while (true) {
                        space();
                        if (peek() != '"') throw bad();
                        String k = string();
                        space();
                        expect(":");
                        m.put(k, value(depth + 1));
                        space();
                        if (peek() == ',') { i++; continue; }
                        expect("}");
                        return m;
                    }
                }
                case '[': {
                    i++;
                    List<Object> a = new ArrayList<>();
                    space();
                    if (peek() == ']') { i++; return a; }
                    while (true) {
                        a.add(value(depth + 1));
                        space();
                        if (peek() == ',') { i++; continue; }
                        expect("]");
                        return a;
                    }
                }
                case '"': return string();
                case 't': expect("true"); return Boolean.TRUE;
                case 'f': expect("false"); return Boolean.FALSE;
                case 'n': expect("null"); return NULL;
                default: return number();
            }
        }

        String string() {
            i++;
            StringBuilder s = new StringBuilder();
            while (true) {
                char c = peek();
                i++;
                if (c == '"') return s.toString();
                if (c < 0x20) throw bad();
                if (c != '\\') { s.append(c); continue; }
                char e = peek();
                i++;
                switch (e) {
                    case '"': case '\\': case '/': s.append(e); break;
                    case 'b': s.append('\b'); break;
                    case 'f': s.append('\f'); break;
                    case 'n': s.append('\n'); break;
                    case 'r': s.append('\r'); break;
                    case 't': s.append('\t'); break;
                    case 'u':
                        if (i + 4 > t.length() || t.charAt(i) == '+' || t.charAt(i) == '-') throw bad();
                        try { s.append((char) Integer.parseInt(t.substring(i, i + 4), 16)); } catch (NumberFormatException x) { throw bad(); }
                        i += 4;
                        break;
                    default: throw bad();
                }
            }
        }

        Double number() {
            int s = i;
            if (i < t.length() && t.charAt(i) == '-') i++;
            if (i < t.length() && t.charAt(i) == '0') i++;
            else if (!digits()) throw bad();
            if (i < t.length() && t.charAt(i) == '.') { i++; if (!digits()) throw bad(); }
            if (i < t.length() && (t.charAt(i) == 'e' || t.charAt(i) == 'E')) {
                i++;
                if (i < t.length() && (t.charAt(i) == '+' || t.charAt(i) == '-')) i++;
                if (!digits()) throw bad();
            }
            return Double.valueOf(t.substring(s, i));
        }

        boolean digits() {
            int s = i;
            while (i < t.length() && t.charAt(i) >= '0' && t.charAt(i) <= '9') i++;
            return i > s;
        }
    }
}
