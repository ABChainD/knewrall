; Python tree-sitter extraction queries for Knewrall CodeGraph
; Captures function/class/method definitions and their relationships.

; ── Definitions ──────────────────────────────────────────────────────────────

; Top-level functions
(module
  (function_definition
    name: (identifier) @def.name
    parameters: (parameters) @def.params
    body: (block
      (expression_statement
        (string) @def.docstring)?)
  ) @def.node
  (#set! def.kind "function"))

; Top-level async functions
(module
  (decorated_definition
    (function_definition
      name: (identifier) @def.name
      parameters: (parameters) @def.params
      body: (block
        (expression_statement
          (string) @def.docstring)?)
    ) @def.node
    (#set! def.kind "function")))

; Classes
(class_definition
  name: (identifier) @def.name
  superclasses: (argument_list)? @def.superclasses
  body: (block
    (expression_statement
      (string) @def.docstring)?)
) @def.node
(#set! def.kind "class")

; Methods inside classes
(class_definition
  name: (identifier) @class.name
  body: (block
    (function_definition
      name: (identifier) @def.name
      parameters: (parameters) @def.params
      body: (block
        (expression_statement
          (string) @def.docstring)?)
    ) @def.node
    (#set! def.kind "method")))

; ── Imports ───────────────────────────────────────────────────────────────────

; import module
(import_statement
  name: (dotted_name) @import.module) @import.node

; from module import ...
(import_from_statement
  module_name: (dotted_name) @import.module
  name: (dotted_name) @import.name) @import.node

; from module import *
(import_from_statement
  module_name: (dotted_name) @import.module) @import.node

; ── Calls ────────────────────────────────────────────────────────────────────

; Simple call: func(...)
(call
  function: (identifier) @call.name) @call.node

; Attribute call: obj.method(...)
(call
  function: (attribute
    attribute: (identifier) @call.name)) @call.node

; ── Inheritance ──────────────────────────────────────────────────────────────

(class_definition
  name: (identifier) @inherit.class
  superclasses: (argument_list
    (identifier) @inherit.parent)) @inherit.node
