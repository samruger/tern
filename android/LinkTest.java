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
            default:                                        // find PIN BASE...
                long t = System.nanoTime();
                for (Link.Answer x : Link.find(Arrays.asList(a).subList(2, a.length), a[1].isEmpty() ? null : a[1]))
                    System.out.println(x.base + " " + (x.pin == null ? "-" : x.pin));
                System.out.println((System.nanoTime() - t) / 1_000_000);
                System.exit(0);
        }
    }
}
