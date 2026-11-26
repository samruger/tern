package tern.desk;

import java.nio.charset.StandardCharsets;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.PrivateKey;
import java.security.Signature;
import java.security.spec.ECGenParameterSpec;
import java.util.Arrays;
import java.util.Map;

/** Act from a command line, for the tests, with software keys in place of the phone's: what each gives, a line each. */
final class ActTest {
    private static KeyPair key() throws Exception {
        KeyPairGenerator g = KeyPairGenerator.getInstance("EC");
        g.initialize(new ECGenParameterSpec("secp256r1"));
        return g.generateKeyPair();
    }

    private static String sign(PrivateKey k, String text) throws Exception {
        Signature s = Signature.getInstance("SHA256withECDSA");
        s.initSign(k);
        s.update(text.getBytes(StandardCharsets.UTF_8));
        return Act.b64(s.sign());
    }

    /** A counter kept in memory; `fail` keeps nothing. */
    private static final class Kept implements Act.Count {
        long next; final boolean fail;
        Kept(long next, boolean fail) { this.next = next; this.fail = fail; }
        @Override public long kept() { return next; }
        @Override public boolean keep(long n) { if (fail) return false; next = n; return true; }
    }

    public static void main(String[] a) throws Exception {
        switch (a[0]) {
            case "sign": {                                  // sign DEVICE TS CMD CODE NAME
                KeyPair e = key(), l = key();
                String eb = Act.b64(e.getPublic().getEncoded()), lb = Act.b64(l.getPublic().getEncoded());
                String payload = Act.payload(a[1], 7, Double.parseDouble(a[2]), a[3]);
                System.out.println(eb);
                System.out.println(lb);
                System.out.println(Act.words(eb, lb));
                System.out.println(payload);
                System.out.println(sign(e.getPrivate(), payload));
                System.out.println(Act.body(payload, sign(l.getPrivate(), payload), true));
                System.out.println(Act.pairBody(a[4], a[5], eb, lb, sign(e.getPrivate(), Act.pairText(a[4], a[5], eb, lb))));
                break;
            }
            case "count": {
                Kept k = new Kept(0, false);
                StringBuilder s = new StringBuilder();
                for (int i = 0; i < 3; i++) s.append(Act.next(k)).append(" ").append(k.next).append(" ");
                System.out.println(s.toString().trim());
                k = new Kept(41, false);
                System.out.println(Act.next(k) + " " + k.next);
                try { System.out.println(Act.next(new Kept(5, true))); } catch (IllegalStateException x) { System.out.println("refused"); }
                break;
            }
            case "quote":
                for (String t : Arrays.copyOfRange(a, 1, a.length)) System.out.println(Act.quote(t));
                break;
            case "command":                                 // whether each is taken for a command, by the check and by payload()
                for (String t : Arrays.copyOfRange(a, 1, a.length)) {
                    boolean signed;
                    try { Act.payload("d", 1, 1, t); signed = true; } catch (IllegalArgumentException x) { signed = false; }
                    System.out.println(signed != Act.command(t) ? "differs" : signed ? "yes" : "no");
                }
                break;
            case "field":                                   // field JSON KEY...
                for (String k : Arrays.copyOfRange(a, 2, a.length)) System.out.println(Act.field(a[1], k));
                break;
            case "cookie":                                  // the app's cookie among these Set-Cookie lines
                System.out.println(Act.appCookie(Arrays.asList(a).subList(1, a.length)));
                break;
            default: {                                      // flow BASE PIN CODE NAME: pairs with a desk, signs in, signs a command
                String base = a[1], pin = a[2];
                KeyPair e = key(), l = key();
                String eb = Act.b64(e.getPublic().getEncoded()), lb = Act.b64(l.getPublic().getEncoded());
                Link.Reply r = Link.post(base, "/app/pair", Act.pairBody(a[3], a[4], eb, lb, sign(e.getPrivate(), Act.pairText(a[3], a[4], eb, lb))), pin, 8000);
                if (r.code != 200) { System.out.println("refused " + r.code + " " + Act.error(r)); return; }
                String offer = Act.field(r.body, "offer");
                System.out.println("offer " + offer + " " + Act.field(r.body, "words") + " / " + Act.words(eb, lb));
                String state = "waiting", device = null;
                for (int i = 0; i < 200 && "waiting".equals(state); i++) {
                    Thread.sleep(100);
                    Link.Reply p = Link.post(base, "/app/paired", Act.offerBody(offer), pin, 8000);
                    state = Act.field(p.body, "state");
                    device = Act.field(p.body, "device");
                }
                System.out.println("state " + state + " " + device);
                Kept count = new Kept(0, false);
                String payload = Act.payload(device, Act.next(count), System.currentTimeMillis() / 1000.0, Act.SESSION);
                Link.Reply s = Link.post(base, "/app/session", Act.body(payload, sign(e.getPrivate(), payload), false), pin, 8000);
                System.out.println("session " + s.code + " " + Act.appCookie(s.cookies) + " " + Act.field(s.body, "name"));
                payload = Act.payload(device, Act.next(count), System.currentTimeMillis() / 1000.0, "{\"op\":\"x\"}");
                System.out.println("act " + Act.body(payload, sign(e.getPrivate(), payload), false));
                payload = Act.payload(device, Act.next(count), System.currentTimeMillis() / 1000.0 - 100, Act.SESSION);
                Link.Reply late = Link.post(base, "/app/session", Act.body(payload, sign(e.getPrivate(), payload), false), pin, 8000);
                System.out.println("late " + late.code + " " + Act.error(late));
                Map<String, Object> m = Act.object(late.body);
                System.out.println("now " + (m.get("now") instanceof Double));
            }
        }
    }
}
