# Empty stacks

An "before" snapshot for analysing infrastructure that does not exist yet. Each file is
an empty CloudFormation template named after a stack in `infrastructure/`, so the diff
engine has something to pair against and every resource appears as an addition.

Stack names must match the synthesised ones: matching is scoped to a stack, so a
mismatch would report every resource as both added and removed.
