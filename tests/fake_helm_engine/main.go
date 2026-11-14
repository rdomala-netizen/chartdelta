// Test stand-in for `helm template` on the appset engine chart: real Go
// text/template semantics, with the few sprig/helm functions the tests use.
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"text/template"
)

func empty(v interface{}) bool {
	if v == nil {
		return true
	}
	rv := reflect.ValueOf(v)
	switch rv.Kind() {
	case reflect.String, reflect.Map, reflect.Slice, reflect.Array:
		return rv.Len() == 0
	case reflect.Bool:
		return !rv.Bool()
	case reflect.Float64, reflect.Float32:
		return rv.Float() == 0
	case reflect.Int, reflect.Int64:
		return rv.Int() == 0
	}
	return false
}

func main() {
	args := os.Args[1:]
	if len(args) < 3 || args[0] != "template" {
		fmt.Fprintln(os.Stderr, "fake engine: unsupported command")
		os.Exit(1)
	}
	chart := args[2]
	vals := map[string]interface{}{}
	for i := 3; i < len(args); i++ {
		if args[i] == "-f" && i+1 < len(args) {
			b, err := os.ReadFile(args[i+1])
			if err != nil {
				fmt.Fprintln(os.Stderr, "Error:", err)
				os.Exit(1)
			}
			m := map[string]interface{}{}
			if err := json.Unmarshal(b, &m); err != nil {
				fmt.Fprintln(os.Stderr, "Error:", err)
				os.Exit(1)
			}
			for k, v := range m {
				vals[k] = v
			}
			i++
		}
	}
	t := template.New("root").Option("missingkey=zero")
	t.Funcs(template.FuncMap{
		"include": func(name string, data interface{}) (string, error) {
			var buf bytes.Buffer
			err := t.ExecuteTemplate(&buf, name, data)
			return buf.String(), err
		},
		"toJson": func(v interface{}) string { b, _ := json.Marshal(v); return string(b) },
		"default": func(d interface{}, given ...interface{}) interface{} {
			if len(given) == 0 || empty(given[0]) {
				return d
			}
			return given[0]
		},
		"quote": func(v interface{}) string { return fmt.Sprintf("%q", fmt.Sprint(v)) },
		"lower": strings.ToLower,
		"upper": strings.ToUpper,
	})
	files, _ := filepath.Glob(filepath.Join(chart, "templates", "*"))
	sort.Strings(files)
	for _, f := range files {
		b, _ := os.ReadFile(f)
		if _, err := t.New(f).Parse(string(b)); err != nil {
			fmt.Fprintln(os.Stderr, "Error: parse error:", err)
			os.Exit(1)
		}
	}
	for _, f := range files {
		if strings.HasPrefix(filepath.Base(f), "_") {
			continue
		}
		var buf bytes.Buffer
		if err := t.ExecuteTemplate(&buf, f, map[string]interface{}{"Values": vals}); err != nil {
			fmt.Fprintln(os.Stderr, "Error: template:", err)
			os.Exit(1)
		}
		out := strings.ReplaceAll(buf.String(), "<no value>", "")
		fmt.Printf("---\n# Source: engine/templates/%s\n%s\n", filepath.Base(f), out)
	}
}
