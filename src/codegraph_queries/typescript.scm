; TypeScript tree-sitter extraction queries for Knewrall CodeGraph
; TypeScript is a superset of JavaScript; most JS queries apply verbatim.

; ── Definitions ──────────────────────────────────────────────────────────────

(function_declaration
  name: (identifier) @def.name
  parameters: (formal_parameters) @def.params
) @def.node
(#set! def.kind "function")

(lexical_declaration
  (variable_declarator
    name: (identifier) @def.name
    value: (arrow_function
      parameters: (formal_parameters) @def.params))) @def.node
(#set! def.kind "function")

(class_declaration
  name: (type_identifier) @def.name
  type_parameters: (type_parameters)? @def.typeparams
) @def.node
(#set! def.kind "class")

; methods
(class_declaration
  name: (type_identifier) @class.name
  body: (class_body
    (method_definition
      name: (property_identifier) @def.name
      parameters: (formal_parameters) @def.params
    ) @def.node
    (#set! def.kind "method")))

; interface declarations treated as class-like
(interface_declaration
  name: (type_identifier) @def.name
) @def.node
(#set! def.kind "class")

; ── Imports ───────────────────────────────────────────────────────────────────

(import_statement
  source: (string) @import.module) @import.node

; ── Calls ────────────────────────────────────────────────────────────────────

(call_expression
  function: (identifier) @call.name) @call.node

(call_expression
  function: (member_expression
    property: (property_identifier) @call.name)) @call.node

; ── Inheritance / implements ─────────────────────────────────────────────────

(class_declaration
  name: (type_identifier) @inherit.class
  superClass: (identifier) @inherit.parent) @inherit.node

(class_declaration
  name: (type_identifier) @inherit.class
  (class_heritage
    (implements_clause
      (type_identifier) @inherit.parent))) @inherit.node
