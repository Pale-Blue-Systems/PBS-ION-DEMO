/*
 * json.c - minimal JSON reader and writer. See json.h.
 *
 * Copyright 2026 the PBS ION demo authors. Licensed under the Apache
 * License, Version 2.0 (see LICENSE at the repository root).
 */
#include "json.h"

#include <errno.h>
#include <inttypes.h>
#include <math.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>

#define JSON_MAX_DEPTH 64

typedef struct {
    const char *start, *p, *end;
    char       *err;
    size_t      errcap;
    int         failed;
} parser_t;

static void perr(parser_t *ps, const char *msg)
{
    if (ps->failed)
        return;
    ps->failed = 1;
    if (ps->err && ps->errcap)
        snprintf(ps->err, ps->errcap, "%s at byte %td", msg, ps->p - ps->start);
}

static void skip_ws(parser_t *ps)
{
    while (ps->p < ps->end &&
           (*ps->p == ' ' || *ps->p == '\t' || *ps->p == '\n' || *ps->p == '\r'))
        ps->p++;
}

static json_value_t *new_value(json_type_t t)
{
    json_value_t *v = calloc(1, sizeof *v);
    if (v)
        v->type = t;
    return v;
}

static json_value_t *parse_value(parser_t *ps, int depth);

static int append_utf8(char **buf, size_t *len, size_t *cap, uint32_t cp)
{
    char tmp[4];
    size_t n;
    if (cp < 0x80) {
        tmp[0] = (char)cp;
        n = 1;
    } else if (cp < 0x800) {
        tmp[0] = (char)(0xC0 | (cp >> 6));
        tmp[1] = (char)(0x80 | (cp & 0x3F));
        n = 2;
    } else if (cp < 0x10000) {
        tmp[0] = (char)(0xE0 | (cp >> 12));
        tmp[1] = (char)(0x80 | ((cp >> 6) & 0x3F));
        tmp[2] = (char)(0x80 | (cp & 0x3F));
        n = 3;
    } else {
        tmp[0] = (char)(0xF0 | (cp >> 18));
        tmp[1] = (char)(0x80 | ((cp >> 12) & 0x3F));
        tmp[2] = (char)(0x80 | ((cp >> 6) & 0x3F));
        tmp[3] = (char)(0x80 | (cp & 0x3F));
        n = 4;
    }
    if (*len + n + 1 > *cap) {
        size_t nc = (*cap ? *cap * 2 : 32) + n;
        char *nb = realloc(*buf, nc);
        if (!nb)
            return -1;
        *buf = nb;
        *cap = nc;
    }
    memcpy(*buf + *len, tmp, n);
    *len += n;
    (*buf)[*len] = '\0';
    return 0;
}

static int hex4(parser_t *ps, uint32_t *out)
{
    if (ps->end - ps->p < 4)
        return -1;
    uint32_t v = 0;
    for (int i = 0; i < 4; i++) {
        char c = ps->p[i];
        v <<= 4;
        if (c >= '0' && c <= '9')
            v |= (uint32_t)(c - '0');
        else if (c >= 'a' && c <= 'f')
            v |= (uint32_t)(c - 'a' + 10);
        else if (c >= 'A' && c <= 'F')
            v |= (uint32_t)(c - 'A' + 10);
        else
            return -1;
    }
    ps->p += 4;
    *out = v;
    return 0;
}

static char *parse_string_raw(parser_t *ps, size_t *outlen)
{
    /* ps->p at the opening quote */
    ps->p++;
    char *buf = NULL;
    size_t len = 0, cap = 0;
    cap = 32;
    buf = malloc(cap);
    if (!buf)
        return NULL;
    buf[0] = '\0';
    while (ps->p < ps->end) {
        unsigned char c = (unsigned char)*ps->p;
        if (c == '"') {
            ps->p++;
            *outlen = len;
            return buf;
        }
        if (c < 0x20) {
            perr(ps, "control character in string");
            free(buf);
            return NULL;
        }
        if (c == '\\') {
            ps->p++;
            if (ps->p >= ps->end)
                break;
            char e = *ps->p++;
            uint32_t cp;
            switch (e) {
            case '"': cp = '"'; break;
            case '\\': cp = '\\'; break;
            case '/': cp = '/'; break;
            case 'b': cp = '\b'; break;
            case 'f': cp = '\f'; break;
            case 'n': cp = '\n'; break;
            case 'r': cp = '\r'; break;
            case 't': cp = '\t'; break;
            case 'u':
                if (hex4(ps, &cp) < 0) {
                    perr(ps, "bad \\u escape");
                    free(buf);
                    return NULL;
                }
                if (cp >= 0xD800 && cp <= 0xDBFF) {
                    uint32_t lo;
                    if (ps->end - ps->p >= 6 && ps->p[0] == '\\' && ps->p[1] == 'u') {
                        ps->p += 2;
                        if (hex4(ps, &lo) < 0 || lo < 0xDC00 || lo > 0xDFFF) {
                            perr(ps, "bad surrogate pair");
                            free(buf);
                            return NULL;
                        }
                        cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                    } else {
                        perr(ps, "lone surrogate");
                        free(buf);
                        return NULL;
                    }
                } else if (cp >= 0xDC00 && cp <= 0xDFFF) {
                    perr(ps, "lone surrogate");
                    free(buf);
                    return NULL;
                }
                break;
            default:
                perr(ps, "bad escape");
                free(buf);
                return NULL;
            }
            if (append_utf8(&buf, &len, &cap, cp) < 0) {
                free(buf);
                return NULL;
            }
            continue;
        }
        /* raw byte (UTF-8 passes through) */
        if (len + 2 > cap) {
            size_t nc = cap * 2 + 2;
            char *nb = realloc(buf, nc);
            if (!nb) {
                free(buf);
                return NULL;
            }
            buf = nb;
            cap = nc;
        }
        buf[len++] = (char)c;
        buf[len] = '\0';
        ps->p++;
    }
    perr(ps, "unterminated string");
    free(buf);
    return NULL;
}

static json_value_t *parse_number(parser_t *ps)
{
    const char *s = ps->p;
    int is_int = 1;
    if (ps->p < ps->end && *ps->p == '-')
        ps->p++;
    if (ps->p >= ps->end || !(*ps->p >= '0' && *ps->p <= '9')) {
        perr(ps, "bad number");
        return NULL;
    }
    if (*ps->p == '0')
        ps->p++;
    else
        while (ps->p < ps->end && *ps->p >= '0' && *ps->p <= '9')
            ps->p++;
    if (ps->p < ps->end && *ps->p == '.') {
        is_int = 0;
        ps->p++;
        if (ps->p >= ps->end || !(*ps->p >= '0' && *ps->p <= '9')) {
            perr(ps, "bad fraction");
            return NULL;
        }
        while (ps->p < ps->end && *ps->p >= '0' && *ps->p <= '9')
            ps->p++;
    }
    if (ps->p < ps->end && (*ps->p == 'e' || *ps->p == 'E')) {
        is_int = 0;
        ps->p++;
        if (ps->p < ps->end && (*ps->p == '+' || *ps->p == '-'))
            ps->p++;
        if (ps->p >= ps->end || !(*ps->p >= '0' && *ps->p <= '9')) {
            perr(ps, "bad exponent");
            return NULL;
        }
        while (ps->p < ps->end && *ps->p >= '0' && *ps->p <= '9')
            ps->p++;
    }
    size_t n = (size_t)(ps->p - s);
    char tmp[64];
    if (n >= sizeof tmp) {
        perr(ps, "number too long");
        return NULL;
    }
    memcpy(tmp, s, n);
    tmp[n] = '\0';
    json_value_t *v = new_value(JSON_NUMBER);
    if (!v)
        return NULL;
    v->number = strtod(tmp, NULL);
    if (is_int) {
        errno = 0;
        long long ll = strtoll(tmp, NULL, 10);
        if (errno == 0) {
            v->is_integer = 1;
            v->integer = (int64_t)ll;
        }
    }
    return v;
}

static int push_item(json_value_t *c, json_value_t *item, char *key)
{
    json_value_t **ni = realloc(c->items, (c->count + 1) * sizeof *ni);
    if (!ni)
        return -1;
    c->items = ni;
    if (c->type == JSON_OBJECT) {
        char **nk = realloc(c->keys, (c->count + 1) * sizeof *nk);
        if (!nk)
            return -1;
        c->keys = nk;
        c->keys[c->count] = key;
    }
    c->items[c->count++] = item;
    return 0;
}

static int match_lit(parser_t *ps, const char *lit)
{
    size_t n = strlen(lit);
    if ((size_t)(ps->end - ps->p) >= n && memcmp(ps->p, lit, n) == 0) {
        ps->p += n;
        return 1;
    }
    return 0;
}

static json_value_t *parse_value(parser_t *ps, int depth)
{
    if (depth > JSON_MAX_DEPTH) {
        perr(ps, "nesting too deep");
        return NULL;
    }
    skip_ws(ps);
    if (ps->p >= ps->end) {
        perr(ps, "unexpected end");
        return NULL;
    }
    char c = *ps->p;
    if (c == '{' || c == '[') {
        json_value_t *v = new_value(c == '{' ? JSON_OBJECT : JSON_ARRAY);
        if (!v)
            return NULL;
        char close = c == '{' ? '}' : ']';
        ps->p++;
        skip_ws(ps);
        if (ps->p < ps->end && *ps->p == close) {
            ps->p++;
            return v;
        }
        for (;;) {
            char *key = NULL;
            if (v->type == JSON_OBJECT) {
                skip_ws(ps);
                if (ps->p >= ps->end || *ps->p != '"') {
                    perr(ps, "expected member name");
                    json_free(v);
                    return NULL;
                }
                size_t kl;
                key = parse_string_raw(ps, &kl);
                if (!key) {
                    json_free(v);
                    return NULL;
                }
                skip_ws(ps);
                if (ps->p >= ps->end || *ps->p != ':') {
                    perr(ps, "expected ':'");
                    free(key);
                    json_free(v);
                    return NULL;
                }
                ps->p++;
            }
            json_value_t *item = parse_value(ps, depth + 1);
            if (!item || push_item(v, item, key) < 0) {
                free(key);
                json_free(item);
                json_free(v);
                return NULL;
            }
            skip_ws(ps);
            if (ps->p < ps->end && *ps->p == ',') {
                ps->p++;
                continue;
            }
            if (ps->p < ps->end && *ps->p == close) {
                ps->p++;
                return v;
            }
            perr(ps, "expected ',' or close");
            json_free(v);
            return NULL;
        }
    }
    if (c == '"') {
        json_value_t *v = new_value(JSON_STRING);
        if (!v)
            return NULL;
        v->string = parse_string_raw(ps, &v->string_len);
        if (!v->string) {
            free(v);
            return NULL;
        }
        return v;
    }
    if (c == '-' || (c >= '0' && c <= '9'))
        return parse_number(ps);
    if (match_lit(ps, "true")) {
        json_value_t *v = new_value(JSON_BOOL);
        if (v)
            v->boolean = 1;
        return v;
    }
    if (match_lit(ps, "false"))
        return new_value(JSON_BOOL);
    if (match_lit(ps, "null"))
        return new_value(JSON_NULL);
    perr(ps, "unexpected character");
    return NULL;
}

json_value_t *json_parse(const char *text, size_t len, char *err, size_t errcap)
{
    parser_t ps = {text, text, text + len, err, errcap, 0};
    if (err && errcap)
        err[0] = '\0';
    json_value_t *v = parse_value(&ps, 0);
    if (!v) {
        if (!ps.failed && err && errcap)
            snprintf(err, errcap, "out of memory");
        return NULL;
    }
    skip_ws(&ps);
    if (ps.p != ps.end) {
        perr(&ps, "trailing data");
        json_free(v);
        return NULL;
    }
    return v;
}

void json_free(json_value_t *v)
{
    if (!v)
        return;
    for (size_t i = 0; i < v->count; i++) {
        json_free(v->items[i]);
        if (v->keys)
            free(v->keys[i]);
    }
    free(v->items);
    free(v->keys);
    free(v->string);
    free(v);
}

const json_value_t *json_get(const json_value_t *obj, const char *key)
{
    if (!obj || obj->type != JSON_OBJECT)
        return NULL;
    for (size_t i = 0; i < obj->count; i++)
        if (strcmp(obj->keys[i], key) == 0)
            return obj->items[i];
    return NULL;
}

const json_value_t *json_at(const json_value_t *arr, size_t i)
{
    if (!arr || arr->type != JSON_ARRAY || i >= arr->count)
        return NULL;
    return arr->items[i];
}

const char *json_get_str(const json_value_t *obj, const char *key)
{
    const json_value_t *v = json_get(obj, key);
    return (v && v->type == JSON_STRING) ? v->string : NULL;
}

int json_get_int(const json_value_t *obj, const char *key, int64_t *out)
{
    const json_value_t *v = json_get(obj, key);
    if (!v || v->type != JSON_NUMBER || !v->is_integer)
        return -1;
    *out = v->integer;
    return 0;
}

int json_get_num(const json_value_t *obj, const char *key, double *out)
{
    const json_value_t *v = json_get(obj, key);
    if (!v || v->type != JSON_NUMBER)
        return -1;
    *out = v->number;
    return 0;
}

int json_get_bool(const json_value_t *obj, const char *key, int def)
{
    const json_value_t *v = json_get(obj, key);
    if (!v || v->type != JSON_BOOL)
        return def;
    return v->boolean;
}

int json_equal(const json_value_t *a, const json_value_t *b)
{
    if (!a || !b)
        return a == b;
    if (a->type != b->type)
        return 0;
    switch (a->type) {
    case JSON_NULL:
        return 1;
    case JSON_BOOL:
        return a->boolean == b->boolean;
    case JSON_NUMBER:
        if (a->is_integer && b->is_integer)
            return a->integer == b->integer;
        return a->number == b->number;
    case JSON_STRING:
        return a->string_len == b->string_len &&
               memcmp(a->string, b->string, a->string_len) == 0;
    case JSON_ARRAY:
        if (a->count != b->count)
            return 0;
        for (size_t i = 0; i < a->count; i++)
            if (!json_equal(a->items[i], b->items[i]))
                return 0;
        return 1;
    case JSON_OBJECT:
        if (a->count != b->count)
            return 0;
        for (size_t i = 0; i < a->count; i++)
            if (!json_equal(a->items[i], json_get(b, a->keys[i])))
                return 0;
        return 1;
    }
    return 0;
}

char *json_read_file(const char *path, size_t *len)
{
    FILE *f = fopen(path, "rb");
    if (!f)
        return NULL;
    char *buf = NULL;
    size_t n = 0, cap = 0;
    for (;;) {
        if (n + 4096 + 1 > cap) {
            size_t nc = cap ? cap * 2 : 65536;
            char *nb = realloc(buf, nc);
            if (!nb) {
                free(buf);
                fclose(f);
                return NULL;
            }
            buf = nb;
            cap = nc;
        }
        size_t r = fread(buf + n, 1, cap - n - 1, f);
        n += r;
        if (r == 0)
            break;
    }
    int bad = ferror(f);
    fclose(f);
    if (bad) {
        free(buf);
        return NULL;
    }
    buf[n] = '\0';
    if (len)
        *len = n;
    return buf;
}

/* ---- Writer ------------------------------------------------------------------------ */

static void w_put(jw_t *w, const char *s, size_t n)
{
    if (w->oom)
        return;
    if (w->len + n + 1 > w->cap) {
        size_t nc = (w->cap ? w->cap * 2 : 1024);
        while (nc < w->len + n + 1)
            nc *= 2;
        char *nb = realloc(w->buf, nc);
        if (!nb) {
            w->oom = 1;
            return;
        }
        w->buf = nb;
        w->cap = nc;
    }
    memcpy(w->buf + w->len, s, n);
    w->len += n;
    w->buf[w->len] = '\0';
}

static void w_puts(jw_t *w, const char *s) { w_put(w, s, strlen(s)); }

static void w_sep(jw_t *w)
{
    if (!w->first)
        w_put(w, ",", 1);
    w->first = 0;
}

void jw_init(jw_t *w) { memset(w, 0, sizeof *w); w->first = 1; }
void jw_free(jw_t *w) { free(w->buf); memset(w, 0, sizeof *w); }

void jw_reset(jw_t *w)
{
    w->len = 0;
    if (w->buf)
        w->buf[0] = '\0';
    w->first = 1;
    w->oom = 0;
}

/* Values written after jw_key must not emit a separator. jw_key sets
 * first = 1 after writing "key":, and every value function calls w_sep. */
void jw_obj_begin(jw_t *w) { w_sep(w); w_put(w, "{", 1); w->first = 1; }
void jw_obj_end(jw_t *w) { w_put(w, "}", 1); w->first = 0; }
void jw_arr_begin(jw_t *w) { w_sep(w); w_put(w, "[", 1); w->first = 1; }
void jw_arr_end(jw_t *w) { w_put(w, "]", 1); w->first = 0; }

/* Length of a valid UTF-8 sequence starting at p (n bytes left), or 0. */
static size_t utf8_valid_len(const unsigned char *p, size_t n)
{
    unsigned char b = p[0];
    size_t need;
    unsigned char lo = 0x80, hi = 0xBF;
    if (b < 0x80)
        return 1;
    if (b >= 0xC2 && b <= 0xDF)
        need = 2;
    else if (b == 0xE0) {
        need = 3;
        lo = 0xA0;
    } else if ((b >= 0xE1 && b <= 0xEC) || b == 0xEE || b == 0xEF)
        need = 3;
    else if (b == 0xED) {
        need = 3;
        hi = 0x9F;
    } else if (b == 0xF0) {
        need = 4;
        lo = 0x90;
    } else if (b >= 0xF1 && b <= 0xF3)
        need = 4;
    else if (b == 0xF4) {
        need = 4;
        hi = 0x8F;
    } else
        return 0;
    if (n < need || p[1] < lo || p[1] > hi)
        return 0;
    for (size_t i = 2; i < need; i++)
        if (p[i] < 0x80 || p[i] > 0xBF)
            return 0;
    return need;
}

/* Bytes that are not valid UTF-8 are written as U+FFFD so that every log
 * line is valid JSON. */
static void w_escaped(jw_t *w, const char *s, size_t n)
{
    w_put(w, "\"", 1);
    for (size_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)s[i];
        char tmp[8];
        if (c >= 0x80) {
            size_t l = utf8_valid_len((const unsigned char *)s + i, n - i);
            if (l == 0) {
                w_put(w, "\\ufffd", 6);
            } else {
                w_put(w, s + i, l);
                i += l - 1;
            }
            continue;
        }
        switch (c) {
        case '"': w_put(w, "\\\"", 2); break;
        case '\\': w_put(w, "\\\\", 2); break;
        case '\n': w_put(w, "\\n", 2); break;
        case '\r': w_put(w, "\\r", 2); break;
        case '\t': w_put(w, "\\t", 2); break;
        default:
            if (c < 0x20 || c == 0x7F) {
                snprintf(tmp, sizeof tmp, "\\u%04x", c);
                w_put(w, tmp, 6);
            } else {
                w_put(w, (const char *)&s[i], 1);
            }
        }
    }
    w_put(w, "\"", 1);
}

void jw_key(jw_t *w, const char *key)
{
    w_sep(w);
    w_escaped(w, key, strlen(key));
    w_put(w, ":", 1);
    w->first = 1;
}

void jw_strn(jw_t *w, const char *s, size_t n) { w_sep(w); w_escaped(w, s, n); }
void jw_str(jw_t *w, const char *s) { if (s) jw_strn(w, s, strlen(s)); else jw_null(w); }

void jw_int(jw_t *w, int64_t v)
{
    char tmp[32];
    w_sep(w);
    snprintf(tmp, sizeof tmp, "%" PRId64, v);
    w_puts(w, tmp);
}

void jw_uint(jw_t *w, uint64_t v)
{
    char tmp[32];
    w_sep(w);
    snprintf(tmp, sizeof tmp, "%" PRIu64, v);
    w_puts(w, tmp);
}

void jw_num(jw_t *w, double v)
{
    char tmp[64];
    w_sep(w);
    if (!isfinite(v)) {
        w_puts(w, "null");
        return;
    }
    snprintf(tmp, sizeof tmp, "%.17g", v);
    w_puts(w, tmp);
}

void jw_bool(jw_t *w, int v) { w_sep(w); w_puts(w, v ? "true" : "false"); }
void jw_null(jw_t *w) { w_sep(w); w_puts(w, "null"); }
void jw_raw(jw_t *w, const char *json) { w_sep(w); w_puts(w, json); }

void jw_kstr(jw_t *w, const char *k, const char *s) { jw_key(w, k); jw_str(w, s); }
void jw_kint(jw_t *w, const char *k, int64_t v) { jw_key(w, k); jw_int(w, v); }
void jw_kuint(jw_t *w, const char *k, uint64_t v) { jw_key(w, k); jw_uint(w, v); }
void jw_knum(jw_t *w, const char *k, double v) { jw_key(w, k); jw_num(w, v); }
void jw_kbool(jw_t *w, const char *k, int v) { jw_key(w, k); jw_bool(w, v); }
void jw_knull(jw_t *w, const char *k) { jw_key(w, k); jw_null(w); }

void jw_khex(jw_t *w, const char *k, const uint8_t *b, size_t n)
{
    static const char d[] = "0123456789abcdef";
    jw_key(w, k);
    w_sep(w);
    w_put(w, "\"", 1);
    for (size_t i = 0; i < n; i++) {
        char t[2] = {d[b[i] >> 4], d[b[i] & 15]};
        w_put(w, t, 2);
    }
    w_put(w, "\"", 1);
}

const char *jw_cstr(jw_t *w) { return w->buf ? w->buf : ""; }
