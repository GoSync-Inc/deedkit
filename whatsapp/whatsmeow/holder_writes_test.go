package main

// ANYM-937: the holder image has a read-only root (deploy/compose.whatsapp<n>-holder.yaml). Every file the adapter
// writes lands in a configured directory: the session directory, the events or pages directory (the spool volume) or
// the socket directory. The process temp directory (os.TempDir, an empty CreateTemp/MkdirTemp directory) is never
// used; SQLite's own temporaries get the overlay's tmpfs /tmp.

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"strings"
	"testing"
)

// spec: ANYM-937
func TestNoAdapterWriteUsesTheProcessTempDirectory(t *testing.T) {
	files, err := os.ReadDir(".")
	if err != nil {
		t.Fatal(err)
	}
	fset := token.NewFileSet()
	checked := 0
	for _, entry := range files {
		name := entry.Name()
		if !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		file, err := parser.ParseFile(fset, name, nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		ast.Inspect(file, func(n ast.Node) bool {
			call, ok := n.(*ast.CallExpr)
			if !ok {
				return true
			}
			fn := ""
			switch f := call.Fun.(type) {
			case *ast.SelectorExpr:
				if pkg, ok := f.X.(*ast.Ident); ok {
					fn = pkg.Name + "." + f.Sel.Name
				}
			case *ast.Ident:
				fn = f.Name
			}
			switch fn {
			case "os.TempDir":
				t.Errorf("%s: os.TempDir: the holder's root is read-only", fset.Position(call.Pos()))
			case "os.CreateTemp", "os.MkdirTemp", "createMediaTemp":
				checked++
				if lit, ok := call.Args[0].(*ast.BasicLit); ok && lit.Value == `""` {
					t.Errorf("%s: %s in the process temp directory", fset.Position(call.Pos()), fn)
				}
			}
			return true
		})
	}
	if checked < 2 { // the media and the history download temporaries (media.go, history.go)
		t.Fatalf("expected the media and history temporaries, saw %d", checked)
	}
	// Both temporaries live in .native-tmp under a configured directory: the pages and the session directory.
	c := testConfig(t)
	for _, parent := range []string{c.PagesDirectory, c.SessionDirectory} {
		dir, err := nativeTempRoot(parent)
		if err != nil || !strings.HasPrefix(dir, parent+string(os.PathSeparator)) {
			t.Fatalf("temp root %q of %q: %v", dir, parent, err)
		}
	}
}
