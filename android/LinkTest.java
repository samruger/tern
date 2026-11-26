package tern.desk;

import java.util.Arrays;

/** Link from a command line, for the tests: what each argument gives, a line each. */
final class LinkTest {
    public static void main(String[] a) {
        switch (a[0]) {
            case "base":
                for (String t : Arrays.copyOfRange(a, 1, a.length)) {
                    try { System.out.println(Link.base(t)); } catch (IllegalArgumentException e) { System.out.println("no"); }
                }
                break;
            case "at":
                for (String u : Arrays.copyOfRange(a, 2, a.length)) System.out.println(Link.at(u, a[1]));
                break;
            case "post":                                    // post PIN BASE PATH BODY
                try {
                    Link.Reply r = Link.post(a[2], a[3], a[4], a[1].isEmpty() ? null : a[1], 4000);
                    System.out.println(r.code);
                    System.out.println(r.body);
                    System.out.println(String.join("|", r.cookies));
                } catch (java.io.IOException e) {
                    System.out.println("no " + e.getMessage());
                }
                break;
            default:                                        // find PIN BASE...
                long t = System.nanoTime();
                for (Link.Answer x : Link.find(Arrays.asList(a).subList(2, a.length), a[1].isEmpty() ? null : a[1]))
                    System.out.println(x.base + " " + (x.pin == null ? "-" : x.pin));
                System.out.println((System.nanoTime() - t) / 1_000_000);
                System.exit(0);
        }
    }
}
