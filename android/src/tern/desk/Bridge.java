package tern.desk;

import android.webkit.JavascriptInterface;

/** What the desk's page may ask of the app, as TernApp: whether this phone is paired, and to sign a command. The app adds
    it only once an approved desk page has loaded, and answers only while one is up. Called on the web view's own thread. */
final class Bridge {
    static final String NAME = "TernApp";

    interface To {
        boolean paired();
        void sign(String id, String cmdJson, boolean live);    // answers with ternSigned(id, body) or ternRefused(id, text)
    }

    private final To to;

    Bridge(To to) { this.to = to; }

    @JavascriptInterface public boolean paired() { return to.paired(); }

    @JavascriptInterface public void sign(String id, String cmdJson, boolean live) { to.sign(id, cmdJson, live); }
}
