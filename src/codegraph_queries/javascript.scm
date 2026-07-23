; JavaScript tree-sitter extraction queries for Knewrall CodeGraph

; ── Definitions ──────────────────────────────────────────────────────────────

; function declarations
(function_declaration
  name: (identifier) @def.name
  parameters: (formal_parameters) @def.params
) @def.node
(#set! def.kind "function")

; arrow function assigned to const/let/var
(lexical_declaration
  (variable_declarator
    name: (identifier) @def.name
    value: (arrow_function
      parameters: (formal_parameters) @def.params))) @def.node
(#set! def.kind "function")

; class declarations
(class_declaration
  name: (identifier) @def.name
  superClass: (identifier)? @def.superclass
) @def.node
(#set! def.kind "class")

; method definitions inside classes
(class_declaration
  name: (identifier) @class.name
  body: (class_body
    (method_definition
      name: (property_identifier) @def.name
      parameters: (formal_parameters) @def.params
    ) @def.node
    (#set! def.kind "method")))

; ── Imports ───────────────────────────────────────────────────────────────────

; import ... from '...'
(import_statement
  source: (string) @import.module) @import.node

; require(...)
(call_expression
  function: (identifier) @_req
  arguments: (arguments (string) @import.module)
  (#eq? @_req "require")) @import.node

; ── Calls ────────────────────────────────────────────────────────────────────

(call_expression
  function: (identifier) @call.name) @call.node

(call_expression
  function: (member_expression
    property: (property_identifier) @call.name)) @call.node

; ── Inheritance ──────────────────────────────────────────────────────────────

(class_declaration
  name: (identifier) @inherit.class
  superClass: (identifier) @inherit.parent) @inherit.node
