package main

import "testing"

func TestANYM801Throwaway(t *testing.T) {
	t.Log("synthetic line")
	t.Fatal("synthetic failure")
}
