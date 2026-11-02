// Statically linked inspector for synthetic OCI runtime tests.
package main

import (
	"encoding/json"
	"fmt"
	"os"
)

func main() {
	result := map[string]json.RawMessage{}
	for _, component := range []string{"base", "app-a", "app-b"} {
		data, err := os.ReadFile("/materials/" + component + ".json")
		if os.IsNotExist(err) {
			continue
		}
		if err != nil || !json.Valid(data) {
			panic("invalid synthetic material")
		}
		result[component] = data
	}
	data, err := json.Marshal(result)
	if err != nil {
		panic(err)
	}
	fmt.Println(string(data))
}
