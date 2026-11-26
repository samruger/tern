package tern.desk;

import android.app.Activity;
import android.content.pm.PackageManager;
import android.hardware.biometrics.BiometricManager;
import android.hardware.biometrics.BiometricPrompt;
import android.os.Build;
import android.os.CancellationSignal;
import android.os.Handler;
import android.os.Looper;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyInfo;
import android.security.keystore.KeyPermanentlyInvalidatedException;
import android.security.keystore.KeyProperties;
import android.security.keystore.UserNotAuthenticatedException;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.KeyFactory;
import java.security.KeyPairGenerator;
import java.security.KeyStore;
import java.security.PrivateKey;
import java.security.ProviderException;
import java.security.Signature;
import java.security.cert.Certificate;
import java.security.spec.ECGenParameterSpec;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.function.Supplier;

/** The phone's two keys, kept in its secure hardware: the everyday one (a fingerprint in the last 5 minutes) and the live
    one (a fingerprint every time). They never leave the phone; the desk has their public halves.
    There are two places for the pair, set 0 and set 1: a new pairing makes its keys in the set not in use, so the pairing the
    phone has keeps working until the desk says yes (the Keystore can't rename a key). */
final class Keys {
    static final String EVERYDAY = "tern.everyday", LIVE = "tern.live";
    static final String GONE = "your fingerprints changed: pair this phone again";
    private static final String STORE = "AndroidKeyStore";
    private static final int WINDOW = 300;                  // s: how long a fingerprint lasts for the everyday key

    /** How a signing ends, on the main thread: one of these, once. */
    interface Done {
        void signed(String text, byte[] sig);
        void refused(String why);
        void gone();                                        // the keys are no longer good: the phone has to be paired again
    }

    private final Activity activity;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private CancellationSignal asking;                      // the fingerprint prompt up now, or null
    private boolean busy, closed;

    Keys(Activity activity) { this.activity = activity; }

    /** With the activity: a prompt up is put away, and nothing more is answered. */
    void close() {
        closed = true;
        if (asking != null) asking.cancel();
        main.removeCallbacksAndMessages(null);
        worker.shutdownNow();
    }

    // ---------------------------------------------------------------- the fingerprint

    /** BiometricManager's answer: BIOMETRIC_SUCCESS, or why a strong fingerprint can't be asked for. */
    @SuppressWarnings("deprecation")
    static int ready(Activity a) {
        BiometricManager m = a.getSystemService(BiometricManager.class);
        if (m == null) return BiometricManager.BIOMETRIC_ERROR_NO_HARDWARE;
        return Build.VERSION.SDK_INT >= 30 ? m.canAuthenticate(BiometricManager.Authenticators.BIOMETRIC_STRONG) : m.canAuthenticate();
    }

    // ---------------------------------------------------------------- making them

    private static KeyStore store() throws GeneralSecurityException, IOException {
        KeyStore s = KeyStore.getInstance(STORE);
        s.load(null);
        return s;
    }

    /** The everyday key's alias in a set (0: the names the first app used, 1: the other place). */
    static String everyday(int set) { return set == 1 ? EVERYDAY + ".1" : EVERYDAY; }

    static String live(int set) { return set == 1 ? LIVE + ".1" : LIVE; }

    private static boolean isLive(String alias) { return LIVE.equals(alias) || live(1).equals(alias); }

    /** Both keys of a set, new, in place of any before in that set; the other set is left as it is. Refused if the phone
        would keep them outside its secure hardware. */
    static void make(Activity a, int set) throws GeneralSecurityException, IOException {
        delete(set);
        boolean strongBox = a.getPackageManager().hasSystemFeature(PackageManager.FEATURE_STRONGBOX_KEYSTORE);
        try {
            make(everyday(set), WINDOW, strongBox);
            make(live(set), 0, strongBox);
        } catch (GeneralSecurityException | IOException | RuntimeException e) {
            delete(set);
            throw e;
        }
    }

    private static void make(String alias, int window, boolean strongBox) throws GeneralSecurityException, IOException {
        try {
            generate(alias, window, strongBox);
        } catch (ProviderException e) {                    // StrongBox can't: the phone's other secure hardware
            if (!strongBox) throw e;
            generate(alias, window, false);
        }
        PrivateKey k = (PrivateKey) store().getKey(alias, null);
        KeyInfo info = KeyFactory.getInstance(k.getAlgorithm(), STORE).getKeySpec(k, KeyInfo.class);
        if (!secure(info)) throw new GeneralSecurityException("this phone can't keep the keys in its secure hardware");
    }

    private static void generate(String alias, int window, boolean strongBox) throws GeneralSecurityException {
        KeyGenParameterSpec.Builder b = new KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_SIGN)
            .setAlgorithmParameterSpec(new ECGenParameterSpec("secp256r1"))
            .setDigests(KeyProperties.DIGEST_SHA256)
            .setUserAuthenticationRequired(true)
            .setInvalidatedByBiometricEnrollment(true)
            .setIsStrongBoxBacked(strongBox);
        fingerprint(b, window);
        KeyPairGenerator g = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, STORE);
        g.initialize(b.build());
        g.generateKeyPair();
    }

    /** A strong fingerprint: within `window` seconds, or (0) for every use. */
    @SuppressWarnings("deprecation")
    private static void fingerprint(KeyGenParameterSpec.Builder b, int window) {
        if (Build.VERSION.SDK_INT >= 30) b.setUserAuthenticationParameters(window, KeyProperties.AUTH_BIOMETRIC_STRONG);
        else if (window > 0) b.setUserAuthenticationValidityDurationSeconds(window);     // left unset, it is every use
    }

    @SuppressWarnings("deprecation")
    private static boolean secure(KeyInfo i) {
        if (Build.VERSION.SDK_INT < 31) return i.isInsideSecureHardware();
        int l = i.getSecurityLevel();
        return l == KeyProperties.SECURITY_LEVEL_TRUSTED_ENVIRONMENT || l == KeyProperties.SECURITY_LEVEL_STRONGBOX || l == KeyProperties.SECURITY_LEVEL_UNKNOWN_SECURE;
    }

    /** Both sets' keys. */
    static void delete() {
        delete(0);
        delete(1);
    }

    static void delete(int set) {
        try {
            KeyStore s = store();
            s.deleteEntry(everyday(set));
            s.deleteEntry(live(set));
        } catch (GeneralSecurityException | IOException e) { /* none to delete */ }
    }

    /** A public key as the desk takes it: X.509 SubjectPublicKeyInfo, base64. */
    static String publicB64(String alias) throws GeneralSecurityException, IOException {
        Certificate c = store().getCertificate(alias);
        if (c == null) throw new KeyPermanentlyInvalidatedException("no " + alias + " key");
        return Act.b64(c.getPublicKey().getEncoded());
    }

    // ---------------------------------------------------------------- signing

    /** Signs what `text` gives, made only once the fingerprint is in (so its time is fresh). Call on the main thread.
        Everyday: the prompt only if the last fingerprint is over 5 minutes old. Live: the prompt every time. */
    void sign(String alias, String title, String why, Supplier<String> text, Done done) {
        if (busy) { done.refused("not sent: another is waiting for your fingerprint"); return; }
        busy = true;
        Done once = new Done() {
            private boolean over;
            private boolean end() { if (over || closed) return false; over = true; busy = false; asking = null; return true; }
            @Override public void signed(String t, byte[] sig) { if (end()) done.signed(t, sig); }
            @Override public void refused(String w) { if (end()) done.refused(w); }
            @Override public void gone() { if (end()) done.gone(); }
        };
        worker.execute(() -> attempt(alias, title, why, text, once, false));
    }

    /** On the worker. */
    private void attempt(String alias, String title, String why, Supplier<String> text, Done done, boolean asked) {
        boolean live = isLive(alias);
        try {
            Signature s = begin(alias);
            if (live) { main.post(() -> ask(title, why, s, () -> worker.execute(() -> end(s, text, done)), done)); return; }
            finish(s, text, done);
        } catch (Exception e) {
            if (!live && !asked && is(e, UserNotAuthenticatedException.class))
                main.post(() -> ask(title, why, null, () -> worker.execute(() -> attempt(alias, title, why, text, done, true)), done));
            else failed(e, done);
        }
    }

    /** On the worker, after the live key's fingerprint. */
    private void end(Signature s, Supplier<String> text, Done done) {
        try {
            finish(s, text, done);
        } catch (Exception e) {
            failed(e, done);
        }
    }

    private static Signature begin(String alias) throws GeneralSecurityException, IOException {
        PrivateKey k = (PrivateKey) store().getKey(alias, null);
        if (k == null) throw new KeyPermanentlyInvalidatedException("no " + alias + " key");
        Signature s = Signature.getInstance("SHA256withECDSA");
        s.initSign(k);
        return s;
    }

    private void finish(Signature s, Supplier<String> text, Done done) throws GeneralSecurityException {
        String t = text.get();
        s.update(t.getBytes(StandardCharsets.UTF_8));
        byte[] sig = s.sign();
        main.post(() -> done.signed(t, sig));
    }

    private void failed(Throwable e, Done done) {
        if (is(e, KeyPermanentlyInvalidatedException.class)) main.post(done::gone);
        else if (is(e, UserNotAuthenticatedException.class)) main.post(() -> done.refused("not sent: your fingerprint wasn't taken"));
        else {
            String m = e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage();
            main.post(() -> done.refused("not sent: " + m));
        }
    }

    private static boolean is(Throwable e, Class<? extends Throwable> kind) {
        for (int i = 0; e != null && i < 8; e = e.getCause(), i++) if (kind.isInstance(e)) return true;
        return false;
    }

    /** The system's fingerprint prompt: strong biometrics only, with the live key's signature bound to it. Main thread. */
    private void ask(String title, String why, Signature crypto, Runnable ok, Done done) {
        if (closed || activity.isFinishing() || activity.isDestroyed()) { done.refused("not sent"); return; }
        BiometricPrompt.Builder b = new BiometricPrompt.Builder(activity).setTitle(title).setDescription(why)
            .setNegativeButton("cancel", activity.getMainExecutor(), (d, w) -> done.refused("not sent"))
            .setConfirmationRequired(crypto != null);
        if (Build.VERSION.SDK_INT >= 30) b.setAllowedAuthenticators(BiometricManager.Authenticators.BIOMETRIC_STRONG);
        BiometricPrompt.AuthenticationCallback said = new BiometricPrompt.AuthenticationCallback() {
            @Override public void onAuthenticationSucceeded(BiometricPrompt.AuthenticationResult r) { asking = null; if (!closed) ok.run(); }
            @Override public void onAuthenticationError(int code, CharSequence text) {
                boolean cancelled = code == BiometricPrompt.BIOMETRIC_ERROR_USER_CANCELED || code == BiometricPrompt.BIOMETRIC_ERROR_CANCELED;   // the cancel button: its own listener
                done.refused(cancelled || text == null ? "not sent" : "not sent: " + text.toString().toLowerCase(Locale.ROOT));
            }
        };
        asking = new CancellationSignal();
        try {
            BiometricPrompt p = b.build();
            if (crypto != null) p.authenticate(new BiometricPrompt.CryptoObject(crypto), asking, activity.getMainExecutor(), said);
            else p.authenticate(asking, activity.getMainExecutor(), said);
        } catch (RuntimeException e) {
            done.refused("not sent: the fingerprint prompt didn't open");
        }
    }
}
