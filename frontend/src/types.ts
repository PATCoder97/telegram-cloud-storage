export type User = {
  id: number;
  username: string;
  role: string;
};

export type Folder = {
  id: number;
  name: string;
};

export type Breadcrumb = {
  id: number | null;
  name: string;
};

export type FileItem = {
  id: number;
  file_name: string;
  formatted_size: string;
  size_bytes: number;
  chunk_amount: number;
  status: string;
  job_id: string | null;
  public_token: string | null;
  error_message: string | null;
  extension: string;
};

export type BrowseResponse = {
  folder_id: number | null;
  current_folder_name: string;
  parent_folder_id: number | null;
  breadcrumbs: Breadcrumb[];
  folders: Folder[];
  files: FileItem[];
  all_user_folders: Folder[];
};
