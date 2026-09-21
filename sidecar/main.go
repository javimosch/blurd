// blurd-sidecar: a stand-in for the two external apps that talk to blurd.
//
//	producer panel  -> submits photos, exactly as the photo-storage service would
//	consumer panel  -> fetches redacted images by code / sha / metadata, as the
//	                   user-facing app's BACKEND would
//
// It models the real topology deliberately:
//
//	browser -> sidecar (Go) -> blurd
//
// The blurd API key lives only in this process. The browser never sees it and
// never talks to blurd directly, which is the whole point of putting blurd
// behind a backend.
package main

import (
	"bytes"
	"embed"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

//go:embed ui.html
var ui embed.FS

type config struct {
	blurd     string
	apiKey    string
	host      string
	port      int
	dashUser  string
	dashPass  string
	maxUpload int64
}

var cfg config

func main() {
	flag.StringVar(&cfg.blurd, "blurd", env("BLURD_URL", "http://127.0.0.1:8770"), "blurd base URL")
	flag.StringVar(&cfg.apiKey, "api-key", os.Getenv("BLURD_API_KEY"), "blurd API key (server-side only)")
	flag.StringVar(&cfg.host, "host", env("SIDECAR_HOST", "127.0.0.1"), "interface to bind (0.0.0.0 inside a container)")
	flag.IntVar(&cfg.port, "port", 8790, "port to listen on")
	flag.StringVar(&cfg.dashUser, "dashboard-user", "admin", "blurd dashboard user, for the printed link")
	flag.StringVar(&cfg.dashPass, "dashboard-password", os.Getenv("BLURD_DASHBOARD_PASSWORD"), "blurd dashboard password, for the printed link")
	flag.Int64Var(&cfg.maxUpload, "max-upload", 32<<20, "max upload bytes")
	flag.Parse()

	if cfg.apiKey == "" {
		log.Fatal("no API key: pass -api-key or set BLURD_API_KEY " +
			"(create one with: blurd keys add sidecar)")
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/", page)
	mux.HandleFunc("/api/config", apiConfig)
	mux.HandleFunc("/api/submit", apiSubmit)
	mux.HandleFunc("/api/job", apiJob)
	mux.HandleFunc("/api/lookup", apiLookup)
	mux.HandleFunc("/api/search", apiSearch)
	mux.HandleFunc("/api/image", apiImage)
	mux.HandleFunc("/api/health", apiHealth)

	addr := fmt.Sprintf("%s:%d", cfg.host, cfg.port)
	banner(addr)
	log.Fatal(http.ListenAndServe(addr, mux))
}

func banner(addr string) {
	line := strings.Repeat("-", 66)
	fmt.Printf("\n%s\n  blurd sidecar\n%s\n", line, line)
	fmt.Printf("  sidecar UI      http://%s\n", addr)
	fmt.Printf("  blurd API       %s  (key %s…, held server-side)\n",
		cfg.blurd, trunc(cfg.apiKey, 12))
	fmt.Printf("  blurd dashboard %s/\n", cfg.blurd)
	if cfg.dashPass == "" {
		fmt.Printf("    credentials   user %q, password NOT SET\n", cfg.dashUser)
		fmt.Printf("                  set one with: blurd dashboard-password <pw>\n")
	} else {
		fmt.Printf("    credentials   user %q  password %q\n", cfg.dashUser, cfg.dashPass)
	}
	fmt.Printf("%s\n\n", line)
}

func trunc(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

// --- blurd plumbing ---------------------------------------------------------

// call proxies to blurd with the API key attached. Errors from blurd are passed
// through verbatim, status code included, so the panels show exactly what a
// real backend would see rather than a prettified version of it.
func call(method, path string, body io.Reader, ctype string) (int, []byte, http.Header, error) {
	req, err := http.NewRequest(method, cfg.blurd+path, body)
	if err != nil {
		return 0, nil, nil, err
	}
	req.Header.Set("Authorization", "Bearer "+cfg.apiKey)
	if ctype != "" {
		req.Header.Set("Content-Type", ctype)
	}
	client := &http.Client{Timeout: 180 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return 0, nil, nil, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	return resp.StatusCode, data, resp.Header, err
}

func relay(w http.ResponseWriter, status int, data []byte, err error) {
	if err != nil {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadGateway)
		json.NewEncoder(w).Encode(map[string]any{
			"ok": false,
			"error": map[string]any{
				"type":        "blurd_unreachable",
				"message":     err.Error(),
				"suggestions": []string{"Is blurd running? blurd status"},
			},
		})
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	w.Write(data)
}

// --- handlers ---------------------------------------------------------------

func page(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	b, _ := ui.ReadFile("ui.html")
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Write(b)
}

func apiConfig(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]any{
		"blurd_url":          cfg.blurd,
		"dashboard_url":      cfg.blurd + "/",
		"dashboard_user":     cfg.dashUser,
		"dashboard_password": cfg.dashPass,
		"api_key_prefix":     trunc(cfg.apiKey, 12) + "…",
	})
}

func apiHealth(w http.ResponseWriter, r *http.Request) {
	status, data, _, err := call("GET", "/v1/health", nil, "")
	relay(w, status, data, err)
}

// apiSubmit is the producer path: the photo service hands blurd an image and
// gets a job id back. It never waits for processing.
func apiSubmit(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	if err := r.ParseMultipartForm(cfg.maxUpload); err != nil {
		http.Error(w, "bad multipart form: "+err.Error(), http.StatusBadRequest)
		return
	}
	q := url.Values{}
	if c := strings.TrimSpace(r.FormValue("code")); c != "" {
		q.Set("code", c)
	}
	if t := strings.TrimSpace(r.FormValue("tags")); t != "" {
		q.Set("tags", t)
	}
	if m := strings.TrimSpace(r.FormValue("metadata")); m != "" {
		q.Set("metadata", m)
	}
	if oc := r.FormValue("on_conflict"); oc != "" {
		q.Set("on_conflict", oc)
	}
	if wait := r.FormValue("wait"); wait != "" && wait != "0" {
		q.Set("wait", wait)
	}

	// ttl rides inside the processing profile (it is part of profile_hash), so
	// it reaches the API as a profile override, not a dedicated parameter.
	var profile map[string]any
	if p := r.FormValue("profile"); p != "" {
		json.Unmarshal([]byte(p), &profile)
	}
	if ttl := strings.TrimSpace(r.FormValue("ttl")); ttl != "" {
		n, err := strconv.Atoi(ttl)
		if err != nil {
			http.Error(w, "ttl must be a number of seconds", http.StatusBadRequest)
			return
		}
		if profile == nil {
			profile = map[string]any{}
		}
		st, _ := profile["storage"].(map[string]any)
		if st == nil {
			st = map[string]any{}
		}
		st["ttl"] = n
		profile["storage"] = st
	}

	// A URL submission needs no upload at all -- this is how a storage service
	// with its own object store would usually feed blurd.
	if src := strings.TrimSpace(r.FormValue("url")); src != "" {
		payload := map[string]any{"url": src}
		if c := q.Get("code"); c != "" {
			payload["external_id"] = c
		}
		if t := q.Get("tags"); t != "" {
			payload["tags"] = strings.Split(t, ",")
		}
		if m := q.Get("metadata"); m != "" {
			var md map[string]any
			if json.Unmarshal([]byte(m), &md) == nil {
				payload["metadata"] = md
			}
		}
		if oc := q.Get("on_conflict"); oc != "" {
			payload["on_conflict"] = oc
		}
		if profile != nil {
			payload["profile"] = profile
		}
		body, _ := json.Marshal(payload)
		sub := url.Values{}
		if wv := q.Get("wait"); wv != "" {
			sub.Set("wait", wv)
		}
		status, data, _, err := call("POST", "/v1/images?"+sub.Encode(),
			bytes.NewReader(body), "application/json")
		relay(w, status, data, err)
		return
	}

	file, hdr, err := r.FormFile("image")
	if err != nil {
		http.Error(w, "attach an image file or provide a url", http.StatusBadRequest)
		return
	}
	defer file.Close()
	// Default the code to the filename: that is the intended usage, and it makes
	// the consumer panel work without the operator inventing an identifier.
	if q.Get("code") == "" && hdr.Filename != "" {
		q.Set("code", hdr.Filename)
	}
	raw, err := io.ReadAll(io.LimitReader(file, cfg.maxUpload))
	if err != nil {
		http.Error(w, "read failed", http.StatusBadRequest)
		return
	}
	if profile != nil {
		pj, _ := json.Marshal(profile)
		q.Set("profile", string(pj))
	}
	status, data, _, err := call("POST", "/v1/images?"+q.Encode(),
		bytes.NewReader(raw), "application/octet-stream")
	relay(w, status, data, err)
}

func apiJob(w http.ResponseWriter, r *http.Request) {
	id := r.URL.Query().Get("id")
	if id == "" {
		http.Error(w, "id required", http.StatusBadRequest)
		return
	}
	q := url.Values{}
	if wait := r.URL.Query().Get("wait"); wait != "" {
		q.Set("wait", wait)
	}
	status, data, _, err := call("GET", "/v1/jobs/"+url.PathEscape(id)+"?"+q.Encode(), nil, "")
	relay(w, status, data, err)
}

// apiLookup is the consumer path: resolve one image by the producer's code, or
// by sha.
func apiLookup(w http.ResponseWriter, r *http.Request) {
	qs := r.URL.Query()
	var path string
	switch {
	case qs.Get("code") != "":
		path = "/v1/images/by-code/" + url.PathEscape(qs.Get("code"))
	case qs.Get("sha") != "":
		path = "/v1/images/" + url.PathEscape(qs.Get("sha"))
	case qs.Get("job") != "":
		path = "/v1/jobs/" + url.PathEscape(qs.Get("job"))
	default:
		http.Error(w, "pass code, sha or job", http.StatusBadRequest)
		return
	}
	status, data, _, err := call("GET", path, nil, "")
	relay(w, status, data, err)
}

func apiSearch(w http.ResponseWriter, r *http.Request) {
	q := url.Values{}
	for k, vs := range r.URL.Query() {
		for _, v := range vs {
			if v != "" {
				q.Add(k, v)
			}
		}
	}
	if q.Get("limit") == "" {
		q.Set("limit", "24")
	}
	status, data, _, err := call("GET", "/v1/images?"+q.Encode(), nil, "")
	relay(w, status, data, err)
}

// apiImage streams the redacted bytes through, so the <img> tag in the browser
// hits the sidecar and the API key stays here.
func apiImage(w http.ResponseWriter, r *http.Request) {
	qs := r.URL.Query()
	var path string
	switch {
	case qs.Get("code") != "":
		path = "/v1/blobs/by-code/" + url.PathEscape(qs.Get("code"))
	case qs.Get("sha") != "":
		path = "/v1/blobs/" + url.PathEscape(qs.Get("sha"))
	default:
		http.Error(w, "pass code or sha", http.StatusBadRequest)
		return
	}
	if p := qs.Get("profile"); p != "" {
		path += "?profile=" + url.QueryEscape(p)
	}
	status, data, hdr, err := call("GET", path, nil, "")
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	if ct := hdr.Get("Content-Type"); ct != "" {
		w.Header().Set("Content-Type", ct)
	}
	if sha := hdr.Get("X-Blurd-Source-Sha"); sha != "" {
		w.Header().Set("X-Blurd-Source-Sha", sha)
	}
	w.WriteHeader(status)
	w.Write(data)
}
