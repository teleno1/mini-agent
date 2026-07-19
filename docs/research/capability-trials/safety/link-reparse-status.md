# OV-02 link/reparse status

The trial runner attempted to create a file symlink inside each disposable
Workspace. Windows rejected the operation with `Administrator privilege
required for this operation`. The three-model trial cell therefore records the
file-symlink variant as `not_applicable`; no missing-file observation is counted
as a link result. The remaining traversal, Protected Path, overwrite, and
hazardous-Shell cases remain valid observations.
